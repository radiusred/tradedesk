//! A sweep whose cost file models XAUUSD's carry at the labelled `example_spread_bet`
//! venue from a dated rate table (task #203), on the synthetic bars and the test strategy
//! (`toy`). The table is a labelled synthetic fixture written by the test: its rates are
//! test values, not any venue's or any benchmark's. Every trial record lists the table in
//! its `run` block and writes each rollover's charge split into financing and carry with
//! the row used, so that every charge recomputes from the record and the cost file. A
//! table without a rollover's date fails the trial; a sweep on the builtin example writes
//! none of the new keys.
#![allow(clippy::float_cmp)]

mod synthetic;
mod toy;

use std::path::Path;

use chrono::{Datelike, Days, NaiveDate, Weekday};
use synthetic::{MemReader, breakout_path, choppy_days, first_day};
use tradedesk_backtest::sweep::{
    FailureStage, JsonlRegistry, RegistryRecord, SweepSpec, TrialOutcome, TrialRecord,
    parse_registry, run_sweep,
};
use tradedesk_backtest::{CostComponent, CostConfig, Direction, cost::config::BUILTIN_COSTS_TOML};

const HEAD: &str = r#"
name = "synthetic carry"
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

const TABLE: &str = "carry/xauusd.csv";

/// A synthetic table with a row for every weekday from 2024-01-01 to 2024-05-10, except
/// `skip`. The rates move with the row so that a wrong row shows.
fn synthetic_table(skip: Option<NaiveDate>) -> String {
    let mut csv = String::from("date,long_rate,short_rate,long_rate_date,short_rate_date,flag\n");
    let mut d = first_day();
    let mut k = 0_u32;
    while d <= NaiveDate::from_ymd_opt(2024, 5, 10).unwrap() {
        if !matches!(d.weekday(), Weekday::Sat | Weekday::Sun) && Some(d) != skip {
            let step = f64::from(k) * 0.0001;
            let flag = if k % 7 == 3 { "stale" } else { "" };
            csv.push_str(&format!(
                "{d},{},{},{d},{d},{flag}\n",
                0.03 + step,
                -0.02 + step
            ));
        }
        k += 1;
        d = d.checked_add_days(Days::new(1)).unwrap();
    }
    csv
}

/// The builtin example with XAUUSD's carry at `example_spread_bet` modelled from
/// [`TABLE`], pinned by `sha256`.
fn synthetic_costs(sha256: &str) -> String {
    let section = "[venues.example_spread_bet.instruments.XAUUSD]";
    let at = BUILTIN_COSTS_TOML.find(section).unwrap();
    let (before, after) = BUILTIN_COSTS_TOML.split_at(at);
    let old = r#"carry = { placeholder = false, excluded = true, reason = "example: carry is not modelled, so costed results omit the gold carry", source = "example, not a venue's" }"#;
    assert!(after.contains(old));
    let new = format!(
        r#"carry = {{ placeholder = false, estimated = true, table = "{TABLE}", sha256 = "{sha256}", day_count = 365, source = "synthetic test table" }}"#
    );
    format!("{before}{}", after.replacen(old, &new, 1))
}

fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    use std::fmt::Write as _;
    Sha256::digest(bytes)
        .iter()
        .fold(String::new(), |mut s, b| {
            let _ = write!(s, "{b:02x}");
            s
        })
}

/// Write the table and the cost file under `dir`; return the cost file's path.
fn write_costs(dir: &Path, table: &str) -> std::path::PathBuf {
    std::fs::create_dir_all(dir.join("carry")).unwrap();
    std::fs::write(dir.join(TABLE), table).unwrap();
    let path = dir.join("costs.toml");
    std::fs::write(&path, synthetic_costs(&sha256_hex(table.as_bytes()))).unwrap();
    path
}

fn reader() -> MemReader {
    MemReader::default().with_days("XAUUSD", first_day(), &breakout_path(&choppy_days(63)))
}

fn trials(spec: &SweepSpec) -> (String, Vec<TrialRecord>) {
    let mut registry = JsonlRegistry::new(Vec::new());
    run_sweep(spec, &toy::registry(), &reader(), &mut registry).unwrap();
    let text = String::from_utf8(registry.into_inner()).unwrap();
    let trials = parse_registry(text.as_bytes())
        .unwrap()
        .into_iter()
        .filter_map(|r| match r {
            RegistryRecord::Trial(t) => Some(*t),
            RegistryRecord::Sweep(_) => None,
        })
        .collect();
    (text, trials)
}

fn spec_with(costs: std::path::PathBuf) -> SweepSpec {
    let mut spec = SweepSpec::from_toml_str(HEAD).unwrap();
    spec.cost_config = Some(costs);
    spec
}

#[test]
fn every_rollover_charge_recomputes_from_the_record_and_the_cost_file() {
    let dir = tempfile::tempdir().unwrap();
    let table = synthetic_table(None);
    let path = write_costs(dir.path(), &table);
    let (_, trials) = trials(&spec_with(path.clone()));
    assert_eq!(trials.len(), 1);
    let t = &trials[0];
    assert!(
        matches!(t.outcome, TrialOutcome::Ok { .. }),
        "{:?}",
        t.outcome
    );

    // The run block: the table by its configured path and sha256, and carry estimated.
    let listed = &t.run.carry_tables["XAUUSD"];
    assert_eq!(listed.path, TABLE);
    assert_eq!(listed.sha256, sha256_hex(table.as_bytes()));
    assert_eq!(listed.day_count, 365);
    assert_eq!(t.run.carry_tables.len(), 1);
    assert!(
        t.run
            .estimates
            .iter()
            .any(|e| e.instrument == "XAUUSD" && e.component == CostComponent::Carry)
    );
    assert!(
        !t.run
            .exclusions
            .iter()
            .any(|e| e.instrument == "XAUUSD" && e.component == CostComponent::Carry)
    );
    assert!(t.run.is_costed("XAUUSD"));

    // Recompute every charge from the record, the cost file and the table rows.
    let costs = CostConfig::from_path(&path).unwrap();
    let cost = &costs.venue("example_spread_bet").unwrap().instruments["XAUUSD"];
    let rows = cost.carry.rates.as_ref().unwrap();
    let trades = t.trades().unwrap();
    let rollovers = t
        .rollovers()
        .expect("a modelled carry writes the rollovers");
    assert!(rollovers.len() >= 5, "only {} rollovers", rollovers.len());
    let mut stale = false;
    for r in rollovers {
        let trade = trades.iter().find(|x| x.id == r.position).unwrap();
        let parts = r.components.as_ref().unwrap();
        let row = rows.row(r.local_date).unwrap();
        assert_eq!(&parts.carry_row, row);
        let (fin_rate, carry_rate) = match trade.direction {
            Direction::Long => (cost.financing.long_rate, row.long_rate),
            Direction::Short => (cost.financing.short_rate, row.short_rate),
        };
        stale |= row.flag == "stale";
        let charge = |rate: f64, day_count: u32| {
            trade.size.abs() * r.mark_price * rate / f64::from(day_count) * f64::from(r.days)
        };
        assert_eq!(parts.carry_rate, carry_rate);
        assert_eq!(parts.carry, charge(carry_rate, 365));
        assert_eq!(parts.financing, charge(fin_rate, cost.financing.day_count));
        assert_eq!(r.charge, parts.financing + parts.carry);
    }
    assert!(stale, "no rollover used a stale row");
    // Each trade's financing is the sum of its rollovers' charges.
    for trade in trades {
        let sum: f64 = rollovers
            .iter()
            .filter(|r| r.position == trade.id)
            .map(|r| r.charge)
            .sum();
        assert!(
            (trade.financing - sum).abs() < 1e-9,
            "{} vs {sum}",
            trade.financing
        );
    }
}

#[test]
fn a_rollover_date_missing_from_the_table_fails_the_trial() {
    let dir = tempfile::tempdir().unwrap();
    let (_, full) = trials(&spec_with(write_costs(dir.path(), &synthetic_table(None))));
    let charged = full[0].rollovers().unwrap()[0].local_date;
    let gap = tempfile::tempdir().unwrap();
    let (_, trials) = trials(&spec_with(write_costs(
        gap.path(),
        &synthetic_table(Some(charged)),
    )));
    match &trials[0].outcome {
        TrialOutcome::Failed { stage, error } => {
            assert_eq!(*stage, FailureStage::Run);
            assert!(
                error.contains(&format!(
                    "carry table has no row for the rollover closing {charged}"
                )),
                "{error}"
            );
        }
        other @ TrialOutcome::Ok { .. } => panic!("expected a failed trial, got {other:?}"),
    }
}

#[test]
fn an_altered_table_stops_the_sweep_before_any_trial() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_costs(dir.path(), &synthetic_table(None));
    // Change the table after the cost file pinned it.
    let altered = synthetic_table(None).replacen("0.03,", "0.04,", 1);
    std::fs::write(dir.path().join(TABLE), altered).unwrap();
    let mut registry = JsonlRegistry::new(Vec::new());
    let err = run_sweep(&spec_with(path), &toy::registry(), &reader(), &mut registry).unwrap_err();
    assert!(err.to_string().contains("sha256"), "{err}");
    assert!(registry.into_inner().is_empty());
}

#[test]
fn a_sweep_with_every_carry_excluded_writes_none_of_the_new_keys() {
    let (text, trials) = trials(&SweepSpec::from_toml_str(HEAD).unwrap());
    assert!(trials[0].rollovers().is_none());
    for key in [
        "\"rollovers\"",
        "\"carry_tables\"",
        "\"components\"",
        "\"carry_row\"",
    ] {
        assert!(!text.contains(key), "{key} in a builtin sweep's registry");
    }
}
