//! A sweep whose cost file gives XAUUSD the volatility slippage and a spread excess
//! (task #195), on the synthetic bars, the test strategy (`toy`) and the labelled
//! `example_spread_bet` venue. The coefficients are synthetic test values, not venue
//! figures. Every record's `run` block lists the estimated components and the cost
//! models; a sweep on the builtin example (fixed slippage, no spread excess) writes none
//! of the new keys. Under the synthetic models every trial records its fills with their
//! detail (task #199), on synthetic minutes with a range and a varying spread so that
//! the charges are not zero.
#![allow(clippy::float_cmp)]

mod synthetic;
mod toy;

use std::collections::BTreeMap;

use synthetic::{MemReader, breakout_path, choppy_days, first_day};
use tradedesk_backtest::engine::{self, EngineConfig, EngineError, SizingPolicy};
use tradedesk_backtest::sweep::{
    JsonlRegistry, RegistryRecord, RegistrySink, SweepSpec, TrialOutcome, parse_registry, run_sweep,
};
use tradedesk_backtest::{
    AccountFx, BarTimeframe, BothHit, CostComponent, CostConfig, EndOfRun, ExitEvaluation,
    LoadRequest, MetricsConfig, OneSidedPolicy, PositionId, SlippageModel, SpreadExcessState,
    cost::config::BUILTIN_COSTS_TOML, load,
};

const HEAD: &str = r#"
name = "synthetic volatility"
strategy = "threshold_cross"
base = "default"
instruments = ["XAUUSD"]
first_day = "2024-01-01"
last_day = "2024-05-02"
venue = "example_spread_bet"
exit_evaluation = { mode = "intrabar", both_hit = "stop_first" }
account_fx = { account_currency = "GBP", rates = { USD = 0.8 } }
sizing = { kind = "stake_per_point", stake = 0.01 }
end_of_run = "close"
starting_capital = 25000.0

[grid]
enter_above = [206000.0]
exit_below = [204000.0]
stop_distance = [5000.0]
"#;

/// The builtin example with XAUUSD at `example_spread_bet` on the synthetic models.
fn synthetic_costs() -> String {
    let section = "[venues.example_spread_bet.instruments.XAUUSD]";
    let at = BUILTIN_COSTS_TOML.find(section).unwrap();
    let (before, after) = BUILTIN_COSTS_TOML.split_at(at);
    let old = r#"slippage = { placeholder = false, excluded = true, reason = "example: no slippage figure, so costed results omit it", source = "example, not a venue's" }"#;
    assert!(after.contains(old));
    let new = r#"slippage = { model = "volatility", market = 0.25, stop = 0.5, limit = 0.0, placeholder = false, estimated = true, source = "synthetic test values" }
spread_excess = { cap = 4.0, placeholder = false, estimated = true, source = "synthetic test value" }"#;
    format!("{before}{}", after.replacen(old, new, 1))
}

fn reader() -> MemReader {
    MemReader::default().with_days("XAUUSD", first_day(), &breakout_path(&choppy_days(63)))
}

fn registry_text(spec: &SweepSpec, reader: &MemReader) -> String {
    let mut registry = JsonlRegistry::new(Vec::new());
    run_sweep(spec, &toy::registry(), reader, &mut registry).unwrap();
    String::from_utf8(registry.into_inner()).unwrap()
}

#[test]
fn every_record_lists_the_estimates_and_the_cost_models() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("synthetic-costs.toml");
    std::fs::write(&path, synthetic_costs()).unwrap();
    let mut spec = SweepSpec::from_toml_str(HEAD).unwrap();
    spec.cost_config = Some(path);
    let reader = reader();
    let text = registry_text(&spec, &reader);
    // The look-back is read with the window: one read per side.
    assert!(
        reader.reads().values().all(|&n| n == 1),
        "{:?}",
        reader.reads()
    );
    let records = parse_registry(text.as_bytes()).unwrap();
    assert_eq!(records.len(), 2);
    for record in &records {
        let run = match record {
            RegistryRecord::Sweep(s) => &s.run,
            RegistryRecord::Trial(t) => {
                assert!(
                    matches!(t.outcome, TrialOutcome::Ok { .. }),
                    "{:?}",
                    t.outcome
                );
                &t.run
            }
        };
        let listed: Vec<(&str, CostComponent, &str)> = run
            .estimates
            .iter()
            .map(|e| (e.instrument.as_str(), e.component, e.source.as_str()))
            .collect();
        assert_eq!(
            listed,
            [
                ("XAUUSD", CostComponent::Slippage, "synthetic test values"),
                (
                    "XAUUSD",
                    CostComponent::SpreadExcess,
                    "synthetic test value"
                ),
            ]
        );
        // Estimated components are costed, and not exclusions or placeholders.
        assert!(run.is_costed("XAUUSD"));
        assert!(run.placeholders.is_empty());
        assert!(
            run.exclusions
                .iter()
                .all(|e| e.instrument != "XAUUSD" || e.component == CostComponent::Carry)
        );
        let models = run.cost_models.as_ref().unwrap();
        assert_eq!(models["XAUUSD"].slippage, SlippageModel::Volatility);
        assert_eq!(models["XAUUSD"].spread_excess, SpreadExcessState::Present);
        assert_eq!(models["EURUSD"].slippage, SlippageModel::Fixed);
        assert_eq!(models["EURUSD"].spread_excess, SpreadExcessState::Absent);
    }
}

#[test]
fn a_fixed_sweep_writes_none_of_the_new_keys() {
    let text = registry_text(&SweepSpec::from_toml_str(HEAD).unwrap(), &reader());
    for key in [
        "\"estimates\"",
        "\"cost_models\"",
        "\"spread_excess\"",
        "\"spread_excess_cost\"",
        "\"fill_kind\"",
        "\"spread_baseline\"",
        "\"half_spread\"",
        "\"estimated\"",
        "\"fills\"",
    ] {
        assert!(!text.contains(key), "{key} in {text}");
    }
}

#[test]
fn a_run_without_minute_costs_fails_and_one_with_them_books_the_detail() {
    let costs = CostConfig::from_toml_str(&synthetic_costs()).unwrap();
    let reader = reader();
    let last = first_day() + chrono::Duration::days(90);
    let config = EngineConfig {
        instrument: "XAUUSD".to_owned(),
        venue: "example_spread_bet".to_owned(),
        exit_evaluation: ExitEvaluation::Intrabar {
            both_hit: BothHit::StopFirst,
        },
        account_fx: AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap(),
        sizing: SizingPolicy::StakePerPoint { stake: 1.0 },
        end_of_run: EndOfRun::Close,
        metrics: MetricsConfig::new(first_day(), last, 25_000.0),
    };
    let request = LoadRequest::utc_days("XAUUSD", first_day(), last, [BarTimeframe::D1]);
    let plain = load(&reader, &[request.clone()], OneSidedPolicy::Reject).unwrap();
    let mut strategy = toy::ThresholdCross::new(toy::ThresholdConfig::default()).unwrap();
    let err = engine::run(&mut strategy, &plain, &costs, &config).unwrap_err();
    assert!(matches!(err, EngineError::NoMinuteCosts { .. }), "{err}");

    let cost = &costs.venue("example_spread_bet").unwrap().instruments["XAUUSD"];
    let lookback = tradedesk_backtest::MinuteLookback::for_cost(cost).unwrap();
    let data = load(
        &reader,
        &[request.with_minute_costs(lookback)],
        OneSidedPolicy::Reject,
    )
    .unwrap();
    let mut strategy = toy::ThresholdCross::new(toy::ThresholdConfig::default()).unwrap();
    let out = engine::run(&mut strategy, &data, &costs, &config).unwrap();
    let fills = out.ledger.fills();
    assert!(!fills.is_empty());
    for f in fills {
        let d = f.cost_detail.expect("every fill carries the detail");
        assert_eq!(d.slippage_model, SlippageModel::Volatility);
        // The synthetic minutes are flat, so every range and every charge is 0.
        assert_eq!(d.range_used, Some(0.0));
        assert_eq!(f.slippage_cost, 0.0);
    }
    for t in out.ledger.closed_trades() {
        assert_eq!(t.spread_excess_cost, Some(0.0));
    }
}

/// The synthetic path on minutes with a 30-cent range and a spread that starts at 40
/// cents and widens by a cent a day, so each minute's spread is above its baseline: the
/// volatility slippage and the spread excess both charge.
fn textured_reader() -> MemReader {
    MemReader::default().with_textured_days(
        "XAUUSD",
        first_day(),
        &breakout_path(&choppy_days(63)),
        30.0,
        |i| 40.0 + f64::from(u16::try_from(i).unwrap()),
    )
}

/// A sweep of `spec` under the synthetic cost models, as registry text.
fn synthetic_registry(spec: &str, reader: &MemReader) -> String {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("synthetic-costs.toml");
    std::fs::write(&path, synthetic_costs()).unwrap();
    let mut spec = SweepSpec::from_toml_str(spec).unwrap();
    spec.cost_config = Some(path);
    registry_text(&spec, reader)
}

#[test]
fn the_registry_writes_every_fill_with_its_detail() {
    let reader = textured_reader();
    let text = synthetic_registry(HEAD, &reader);
    let records = parse_registry(text.as_bytes()).unwrap();
    let RegistryRecord::Trial(trial) = &records[1] else {
        panic!("{:?}", records[1]);
    };
    let fills = trial.fills().expect("the fills are recorded");
    let trades = trial.trades().unwrap();

    // The same cell run directly: the registry holds the ledger's fills, field for field.
    let costs = CostConfig::from_toml_str(&synthetic_costs()).unwrap();
    let spec = SweepSpec::from_toml_str(HEAD).unwrap();
    let config = EngineConfig {
        instrument: "XAUUSD".to_owned(),
        venue: "example_spread_bet".to_owned(),
        exit_evaluation: spec.exit_evaluation,
        account_fx: AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap(),
        sizing: SizingPolicy::StakePerPoint { stake: 0.01 },
        end_of_run: EndOfRun::Close,
        metrics: MetricsConfig::new(spec.first_day, spec.last_day, 25_000.0),
    };
    let cost = &costs.venue("example_spread_bet").unwrap().instruments["XAUUSD"];
    let request =
        LoadRequest::utc_days("XAUUSD", spec.first_day, spec.last_day, [BarTimeframe::D1])
            .with_minute_costs(tradedesk_backtest::MinuteLookback::for_cost(cost).unwrap());
    let data = load(&reader, &[request], OneSidedPolicy::Reject).unwrap();
    let mut strategy =
        toy::ThresholdCross::new(serde_json::from_value(trial.config.clone().unwrap()).unwrap())
            .unwrap();
    let out = engine::run(&mut strategy, &data, &costs, &config).unwrap();
    assert_eq!(fills, out.ledger.fills());
    assert_eq!(trades, out.ledger.closed_trades());

    // The charges are not zero, so the sums below test something.
    assert!(fills.iter().all(|f| f.cost_detail.is_some()));
    assert!(fills.iter().any(|f| f.slippage_cost > 0.0));
    assert!(
        fills
            .iter()
            .any(|f| f.cost_detail.unwrap().spread_excess_cost > 0.0)
    );

    // Each fill links to its trade; per trade, the fills sum to the trade's charges.
    let mut by_trade: BTreeMap<PositionId, (usize, f64, f64)> = BTreeMap::new();
    for f in fills {
        let sums = by_trade.entry(f.position).or_default();
        sums.0 += 1;
        sums.1 += f.slippage_cost;
        sums.2 += f.cost_detail.unwrap().spread_excess_cost;
    }
    assert_eq!(by_trade.len(), trades.len());
    for t in trades {
        let (n, slippage, excess) = by_trade[&t.id];
        assert_eq!(n, 2, "trade {:?}: an entry and an exit", t.id);
        assert!((slippage - t.slippage_cost).abs() < 1e-9, "{t:?}");
        assert!(
            (excess - t.spread_excess_cost.unwrap()).abs() < 1e-9,
            "{t:?}"
        );
    }
}

#[test]
fn a_registry_line_with_fills_round_trips() {
    let text = synthetic_registry(HEAD, &textured_reader());
    assert!(text.contains("\"fills\""));
    let records = parse_registry(text.as_bytes()).unwrap();
    let mut again = JsonlRegistry::new(Vec::new());
    for record in &records {
        again.write(record).unwrap();
    }
    let again = String::from_utf8(again.into_inner()).unwrap();
    assert_eq!(again, text);
    assert_eq!(parse_registry(again.as_bytes()).unwrap(), records);
}

/// `records` written back with every trial's `fills` taken out.
fn without_fills(records: &[RegistryRecord]) -> String {
    let mut out = JsonlRegistry::new(Vec::new());
    for record in records {
        let mut record = record.clone();
        if let RegistryRecord::Trial(t) = &mut record {
            if let TrialOutcome::Ok { fills, .. } = &mut t.outcome {
                assert!(fills.take().is_some());
            }
        }
        out.write(&record).unwrap();
    }
    String::from_utf8(out.into_inner()).unwrap()
}

#[cfg(feature = "cli")]
#[test]
fn the_report_and_the_walkforward_ignore_the_fills() {
    use std::sync::atomic::AtomicBool;
    use tradedesk_backtest::cli;

    let spec = HEAD.replace(
        "stop_distance = [5000.0]",
        "stop_distance = [5000.0, 8000.0]",
    );
    let with = synthetic_registry(&spec, &textured_reader());
    let records = parse_registry(with.as_bytes()).unwrap();
    assert_eq!(records.len(), 3);
    let without = without_fills(&records);
    assert!(with.contains("\"fills\"") && !without.contains("\"fills\""));

    let dir = tempfile::tempdir().unwrap();
    let run = |text: &str, args: &[&str]| {
        let path = dir.path().join("registry.jsonl");
        std::fs::write(&path, text).unwrap();
        let mut stdout = Vec::new();
        let code = cli::run(
            ["tradedesk-backtest"]
                .iter()
                .chain(args)
                .map(std::ffi::OsString::from)
                .chain([path.into_os_string()]),
            &toy::registry(),
            &mut stdout,
            &AtomicBool::new(false),
        );
        assert_eq!(code, 0, "{args:?}");
        String::from_utf8(stdout).unwrap()
    };
    let months = [
        "--train-months",
        "1",
        "--test-months",
        "1",
        "--step-months",
        "1",
    ];
    for args in [
        vec!["report"],
        vec!["report", "--format", "json"],
        [&["walkforward"][..], &months].concat(),
        [&["walkforward", "--format", "json"][..], &months].concat(),
    ] {
        let a = run(&with, &args);
        assert!(!a.is_empty(), "{args:?}");
        assert_eq!(a, run(&without, &args), "{args:?}");
    }
}
