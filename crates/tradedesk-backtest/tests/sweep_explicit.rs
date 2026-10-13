//! `base = "explicit"` (#184): each cell is the whole strategy config. A complete cell
//! runs and its values are the trial's recorded config; a cell that omits a field, a
//! field with a serde default included, is refused with the path before any trial
//! runs; an unknown path is refused; a grid expands to complete cells. The strategy is
//! the test strategy (`toy`), on synthetic bars.
#![allow(clippy::float_cmp)]

mod synthetic;
mod toy;

use std::collections::BTreeMap;

use serde_json::{Value, json};
use synthetic::{MemReader, breakout_path, choppy_days, first_day};
use tradedesk_backtest::sweep::{
    BaseConfig, BoxedStrategy, JsonlRegistry, RegistryRecord, RegistrySink, StrategyFactory,
    SweepSpec, TrialOutcome, TrialRecord, read_registry, run_sweep, validate_spec,
};

/// The spec without its base and cells.
const HEAD: &str = r#"
strategy = "threshold_cross"
instruments = ["XAUUSD"]
first_day = "2024-01-01"
last_day = "2024-05-02"
venue = "example_spread_bet"
exit_evaluation = { mode = "intrabar", both_hit = "stop_first" }
account_fx = { account_currency = "GBP", rates = { USD = 0.8 } }
sizing = { kind = "stake_per_point", stake = 0.01 }
end_of_run = "close"
starting_capital = 25000.0
"#;

/// A complete cell for the test strategy. Every value differs from the strategy's
/// default config (`cooldown_bars` defaults to 1, `exit_below` to 203000, and so on), so
/// a value taken from the default would show. `price_scale` is derived, not set.
fn complete_cell() -> BTreeMap<String, Value> {
    [
        ("timeframe", json!("1d")),
        ("enter_above", json!(206_000.0)),
        ("exit_below", json!(204_000.0)),
        ("stop_distance", json!(5_000.0)),
        ("target_distance", json!(8_000.0)),
        ("cooldown_bars", json!(2)),
    ]
    .into_iter()
    .map(|(k, v)| (k.to_owned(), v))
    .collect()
}

/// The every-field set of the test strategy's config, less the derived `price_scale`.
const FIELDS: [&str; 6] = [
    "cooldown_bars",
    "enter_above",
    "exit_below",
    "stop_distance",
    "target_distance",
    "timeframe",
];

fn spec(base: &str, cells: Vec<BTreeMap<String, Value>>) -> SweepSpec {
    let mut spec = SweepSpec::from_toml_str(&format!("base = \"{base}\"\n{HEAD}")).unwrap();
    spec.cells = cells;
    spec
}

fn reader() -> MemReader {
    MemReader::default().with_days("XAUUSD", first_day(), &breakout_path(&choppy_days(63)))
}

fn trials(records: &[RegistryRecord]) -> Vec<&TrialRecord> {
    records
        .iter()
        .filter_map(|r| match r {
            RegistryRecord::Trial(t) => Some(t.as_ref()),
            RegistryRecord::Sweep(_) => None,
        })
        .collect()
}

/// The config a trial on XAUUSD records for `cell`: the cell, plus the derived scale.
fn expected_config(cell: &BTreeMap<String, Value>) -> Value {
    let mut config: serde_json::Map<String, Value> =
        cell.iter().map(|(k, v)| (k.clone(), v.clone())).collect();
    let raw_scale = tradedesk_backtest::CostConfig::builtin()
        .instrument("XAUUSD")
        .unwrap()
        .raw_scale;
    config.insert("price_scale".into(), json!(raw_scale));
    Value::Object(config)
}

/// Writes every record to two sinks.
struct Tee<'a>(&'a mut Vec<RegistryRecord>, &'a mut dyn RegistrySink);

impl RegistrySink for Tee<'_> {
    fn write(&mut self, record: &RegistryRecord) -> std::io::Result<()> {
        self.0.write(record)?;
        self.1.write(record)
    }
}

#[test]
fn a_complete_cell_is_accepted_and_round_trips_into_the_trial_record() {
    let strategies = toy::registry();
    let spec = spec("explicit", vec![complete_cell()]);
    assert_eq!(spec.base, BaseConfig::Explicit);
    validate_spec(&spec, &strategies).unwrap();
    // The spec round-trips through TOML with its base.
    let text = toml::to_string(&spec).unwrap();
    assert!(text.contains("base = \"explicit\""), "{text}");
    assert_eq!(SweepSpec::from_toml_str(&text).unwrap(), spec);

    let path = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join("sweep_explicit.jsonl");
    let _ = std::fs::remove_file(&path);
    let mut records = Vec::new();
    {
        let mut file = JsonlRegistry::append(&path).unwrap();
        let summary = run_sweep(
            &spec,
            &strategies,
            &reader(),
            &mut Tee(&mut records, &mut file),
        )
        .unwrap();
        assert_eq!((summary.cells, summary.ok, summary.failed), (1, 1, 0));
    }
    assert_eq!(read_registry(&path).unwrap(), records);
    let trials = trials(&records);
    let [trial] = trials.as_slice() else {
        panic!("one trial");
    };
    assert!(matches!(trial.outcome, TrialOutcome::Ok { .. }));
    assert_eq!(trial.params, complete_cell());
    // Every field is the cell's value; nothing came from the default config.
    assert_eq!(
        trial.config.as_ref().unwrap(),
        &expected_config(&complete_cell())
    );
}

#[test]
fn a_cell_missing_a_field_is_refused_with_its_path_before_any_trial_runs() {
    let strategies = toy::registry();
    for field in FIELDS {
        let mut cell = complete_cell();
        cell.remove(field);
        let spec = spec("explicit", vec![complete_cell(), cell]);
        let err = validate_spec(&spec, &strategies).unwrap_err().to_string();
        assert!(err.contains(&format!("{field:?}")), "{field}: {err}");
        assert!(err.contains("cell 1"), "{field}: {err}");
        // The sweep refuses the same way: nothing is loaded or written.
        let reader = reader();
        let mut records = Vec::new();
        let run = run_sweep(&spec, &strategies, &reader, &mut records).unwrap_err();
        assert_eq!(run.to_string(), err);
        assert!(records.is_empty() && reader.reads().is_empty(), "{field}");
    }
    // Every missing path is named.
    let mut cell = complete_cell();
    cell.remove("exit_below");
    cell.remove("timeframe");
    let err = validate_spec(&spec("explicit", vec![cell]), &strategies)
        .unwrap_err()
        .to_string();
    assert!(err.contains(r#"["exit_below", "timeframe"]"#), "{err}");
}

#[test]
fn a_field_with_a_serde_default_is_still_required() {
    // `cooldown_bars` has a serde default: the strategy itself builds without it.
    let strategies = toy::registry();
    let factory = strategies.get(toy::NAME).unwrap();
    let mut config = expected_config(&complete_cell());
    config.as_object_mut().unwrap().remove("cooldown_bars");
    assert!(factory.build(config).is_ok());
    // Under explicit base a cell that omits it is refused all the same.
    let mut cell = complete_cell();
    cell.remove("cooldown_bars");
    let err = validate_spec(&spec("explicit", vec![cell.clone()]), &strategies)
        .unwrap_err()
        .to_string();
    assert!(err.contains("\"cooldown_bars\""), "{err}");
    // Under the default base the same cell takes the value from the base.
    let spec = spec("default", vec![cell]);
    validate_spec(&spec, &strategies).unwrap();
    let mut records = Vec::new();
    run_sweep(&spec, &strategies, &reader(), &mut records).unwrap();
    assert_eq!(
        trials(&records)[0].config.as_ref().unwrap()["cooldown_bars"],
        1
    );
}

#[test]
fn an_unknown_path_is_refused() {
    let strategies = toy::registry();
    for (path, value) in [
        ("enter_abov", json!(1.0)),
        ("enter_above.level", json!(1.0)),
        ("", json!(1.0)),
    ] {
        let mut cell = complete_cell();
        cell.insert(path.into(), value);
        let err = validate_spec(&spec("explicit", vec![cell]), &strategies)
            .unwrap_err()
            .to_string();
        assert!(err.contains(&format!("{path:?}")), "{path}: {err}");
    }
    // The derived price scale stays derived.
    let mut cell = complete_cell();
    cell.insert("price_scale".into(), json!(100.0));
    let err = validate_spec(&spec("explicit", vec![cell]), &strategies)
        .unwrap_err()
        .to_string();
    assert!(
        err.contains("\"price_scale\"") && err.contains("raw_scale"),
        "{err}"
    );
}

#[test]
fn a_grid_under_explicit_base_expands_to_complete_cells() {
    let strategies = toy::registry();
    let mut spec = spec("explicit", vec![]);
    for (k, v) in complete_cell() {
        spec.grid.insert(k, vec![v]);
    }
    spec.grid
        .insert("stop_distance".into(), vec![json!(5_000.0), json!(6_000.0)]);
    spec.grid.insert(
        "target_distance".into(),
        vec![json!(8_000.0), json!(9_000.0)],
    );
    validate_spec(&spec, &strategies).unwrap();
    let mut records = Vec::new();
    let summary = run_sweep(&spec, &strategies, &reader(), &mut records).unwrap();
    assert_eq!((summary.cells, summary.ok), (4, 4));
    let mut pairs = Vec::new();
    for t in trials(&records) {
        assert_eq!(
            t.params.keys().map(String::as_str).collect::<Vec<_>>(),
            FIELDS
        );
        assert_eq!(t.config.as_ref().unwrap(), &expected_config(&t.params));
        pairs.push((
            t.params["stop_distance"].clone(),
            t.params["target_distance"].clone(),
        ));
    }
    assert_eq!(
        pairs,
        [
            (json!(5_000.0), json!(8_000.0)),
            (json!(5_000.0), json!(9_000.0)),
            (json!(6_000.0), json!(8_000.0)),
            (json!(6_000.0), json!(9_000.0)),
        ]
    );
    // A grid that leaves a field out is refused with it.
    spec.grid.remove("cooldown_bars");
    let err = validate_spec(&spec, &strategies).unwrap_err().to_string();
    assert!(err.contains("\"cooldown_bars\""), "{err}");
    // No grid and no cells: one empty cell, which sets nothing.
    spec.grid.clear();
    let err = validate_spec(&spec, &strategies).unwrap_err().to_string();
    for field in FIELDS {
        assert!(err.contains(&format!("{field:?}")), "{field}: {err}");
    }
}

/// A strategy with a frozen config and no default one.
struct FrozenOnly;

impl StrategyFactory for FrozenOnly {
    fn base_config(&self, base: BaseConfig) -> Result<Value, String> {
        match base {
            BaseConfig::Frozen => Ok(json!({ "period": 20 })),
            BaseConfig::Default | BaseConfig::Explicit => Err("no default config".into()),
        }
    }

    fn build(&self, config: Value) -> Result<BoxedStrategy, String> {
        Err(format!("not built in this test: {config}"))
    }
}

#[test]
fn explicit_base_needs_the_strategys_default_config_for_its_fields() {
    let mut strategies = toy::registry();
    strategies.register("frozen_only", FrozenOnly).unwrap();
    let mut spec = spec(
        "explicit",
        vec![BTreeMap::from([("period".into(), json!(10))])],
    );
    spec.strategy = "frozen_only".into();
    let err = validate_spec(&spec, &strategies).unwrap_err().to_string();
    assert!(
        err.contains("default config") && err.contains("explicit"),
        "{err}"
    );
}
