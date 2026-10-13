//! The spec's `[provenance]` table (#188): echoed verbatim into the sweep record and
//! every trial record, in the sweep id and in no trial id, and absent from every line
//! when the spec has none. On the synthetic bars, the test strategy (`toy`) and the
//! labelled `example_spread_bet` venue.

mod synthetic;
mod toy;

use std::collections::BTreeMap;

use synthetic::{MemReader, breakout_path, choppy_days, first_day};
use tradedesk_backtest::sweep::{
    JsonlRegistry, RegistryRecord, SweepSpec, TrialOutcome, TrialRecord, parse_registry, run_sweep,
    validate_spec,
};

/// One instrument, two cells: a $50 stop that trades and the invalid 0 that fails.
const HEAD: &str = r#"
name = "synthetic provenance"
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
stop_distance = [5000.0, 0.0]
"#;

/// Keys the engine has never heard of, a dotted and a spaced key, non-ASCII, quotes, a
/// backslash and an empty value.
const PROVENANCE: &str = r#"
[provenance]
registration_sha256 = "0000000000000000000000000000000000000000000000000000000000000000"
"document.path" = "plans/example plan.md"
"free text" = "say \"hi\" \\ café"
empty = ""
"#;

/// The golden: the table exactly as every JSON Lines record writes it, keys sorted.
const GOLDEN: &str = concat!(
    r#""provenance":{"document.path":"plans/example plan.md","empty":"","#,
    r#""free text":"say \"hi\" \\ café","#,
    r#""registration_sha256":"0000000000000000000000000000000000000000000000000000000000000000"}"#,
);

/// The field's JSON key, on any line that has the table.
const KEY: &str = r#""provenance":"#;

fn spec(provenance: bool) -> SweepSpec {
    let text = if provenance {
        format!("{HEAD}{PROVENANCE}")
    } else {
        HEAD.to_owned()
    };
    SweepSpec::from_toml_str(&text).unwrap()
}

fn reader() -> MemReader {
    MemReader::default().with_days("XAUUSD", first_day(), &breakout_path(&choppy_days(63)))
}

/// The sweep's JSON Lines text.
fn registry_text(spec: &SweepSpec) -> String {
    let mut registry = JsonlRegistry::new(Vec::new());
    run_sweep(spec, &toy::registry(), &reader(), &mut registry).unwrap();
    String::from_utf8(registry.into_inner()).unwrap()
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

fn ids(records: &[RegistryRecord]) -> (String, Vec<String>) {
    let RegistryRecord::Sweep(header) = &records[0] else {
        panic!("a header first");
    };
    let trials = trials(records).iter().map(|t| t.trial_id.clone()).collect();
    (header.sweep_id.clone(), trials)
}

#[test]
fn the_table_is_echoed_verbatim_into_every_record() {
    let spec = spec(true);
    assert_eq!(spec.provenance.len(), 4);
    assert_eq!(spec.provenance["free text"], "say \"hi\" \\ café");
    let text = registry_text(&spec);
    let lines: Vec<&str> = text.lines().collect();
    assert_eq!(lines.len(), 3, "a header and two trials");
    // The golden: the header (inside `spec`) and each trial, the failed one included,
    // write the table byte for byte the same, once.
    for line in &lines {
        assert_eq!(line.matches(GOLDEN).count(), 1, "{line}");
        assert_eq!(line.matches(KEY).count(), 1, "{line}");
    }
    assert!(lines[0].starts_with(r#"{"kind":"sweep""#));
    // Read back, the records carry the table as given.
    let records = parse_registry(text.as_bytes()).unwrap();
    let RegistryRecord::Sweep(header) = &records[0] else {
        panic!("a header first");
    };
    assert_eq!(header.spec.provenance, spec.provenance);
    let trials = trials(&records);
    assert_eq!(trials.len(), 2);
    assert!(matches!(trials[0].outcome, TrialOutcome::Ok { .. }));
    assert!(matches!(trials[1].outcome, TrialOutcome::Failed { .. }));
    for t in &trials {
        assert_eq!(t.provenance, spec.provenance);
    }
}

#[test]
fn without_a_table_nothing_is_written_and_no_trial_id_moves() {
    let without = registry_text(&spec(false));
    assert!(!without.contains(KEY), "{without}");
    let without = parse_registry(without.as_bytes()).unwrap();
    for t in trials(&without) {
        assert!(t.provenance.is_empty());
    }
    let with = parse_registry(registry_text(&spec(true)).as_bytes()).unwrap();
    let (sweep_without, trials_without) = ids(&without);
    let (sweep_with, trials_with) = ids(&with);
    // The table is not an input to any result: the trial ids are the same.
    assert_eq!(trials_without, trials_with);
    // It is part of the spec, so the sweep id hashes it.
    assert_ne!(sweep_without, sweep_with);
    // An empty table is the same as none.
    let mut empty = spec(false);
    empty.provenance = BTreeMap::new();
    assert!(!registry_text(&empty).contains(KEY));
}

#[test]
fn values_are_strings_and_keys_are_free() {
    // Any key validates; the engine reads none of them.
    let mut spec = spec(false);
    spec.provenance
        .insert("anything.at all/★".into(), "x".into());
    assert!(validate_spec(&spec, &toy::registry()).is_ok());
    // The spec round-trips through TOML with its table.
    let text = toml::to_string(&spec).unwrap();
    assert_eq!(SweepSpec::from_toml_str(&text).unwrap(), spec);
    // A value that is not a string is a parse error.
    for bad in [
        "[provenance]\nn = 1\n",
        "[provenance]\nb = true\n",
        "[provenance]\na = [\"x\"]\n",
        "[provenance.nested]\nk = \"v\"\n",
    ] {
        let err = SweepSpec::from_toml_str(&format!("{HEAD}{bad}"));
        assert!(err.is_err(), "{bad} was accepted");
    }
}

#[test]
fn a_trial_line_written_before_the_field_reads_back_empty() {
    let text = registry_text(&spec(true));
    let trial_line = text.lines().nth(1).unwrap();
    let older = trial_line.replacen(&format!("{GOLDEN},"), "", 1);
    assert!(!older.contains(KEY), "{older}");
    let records = parse_registry(format!("{older}\n").as_bytes()).unwrap();
    let RegistryRecord::Trial(t) = &records[0] else {
        panic!("a trial");
    };
    assert!(t.provenance.is_empty());
}
