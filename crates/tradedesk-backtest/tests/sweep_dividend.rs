//! A sweep whose cost file charges a dividend on XAUUSD at the labelled
//! `example_spread_bet` venue from an ex-date table (task #209), on the synthetic bars and
//! the test strategy (`toy`). The engine does not tie the component to an asset class;
//! the test uses the instrument the synthetic bars exist for. The table is a labelled
//! synthetic fixture written by the test: its points and shares are test values, not any
//! index's or any venue's. Every trial record lists the table in its `run` block and
//! writes each dividend charge as its own held-position record with its `dividend` part,
//! so that every charge recomputes from the record and the cost file. A position held
//! over a date the table does not cover fails the trial.
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
name = "synthetic dividend"
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

const TABLE: &str = "dividend/xauusd.csv";

/// A synthetic table with a row for every weekday from 2024-01-01 to `last`. The points
/// move with the row so that a wrong row shows; every fifth row is 0 and every seventh a
/// bound with a short share above 1.
fn synthetic_table(last: NaiveDate) -> String {
    let mut csv = String::from("date,points,long_share,short_share,bound\n");
    let mut d = first_day();
    let mut k = 0_u32;
    while d <= last {
        if !matches!(d.weekday(), Weekday::Sat | Weekday::Sun) {
            let points = if k % 5 == 0 { 0.0 } else { 10.0 + f64::from(k) };
            let (short_share, bound) = if k % 7 == 3 {
                (1.2, true)
            } else {
                (1.0, false)
            };
            csv.push_str(&format!("{d},{points},0.9,{short_share},{bound}\n"));
        }
        k += 1;
        d = d.checked_add_days(Days::new(1)).unwrap();
    }
    csv
}

fn full_table() -> String {
    synthetic_table(NaiveDate::from_ymd_opt(2024, 5, 10).unwrap())
}

/// The builtin example with a dividend on XAUUSD at `example_spread_bet` from [`TABLE`],
/// pinned by `sha256`, charged at 16:00 New York on the weekday before each ex-date.
fn synthetic_costs(sha256: &str) -> String {
    let section = "[venues.example_spread_bet.instruments.XAUUSD]";
    let at = BUILTIN_COSTS_TOML.find(section).unwrap();
    let (before, after) = BUILTIN_COSTS_TOML.split_at(at);
    let line = format!(
        r#"dividend = {{ placeholder = false, estimated = true, table = "{TABLE}", sha256 = "{sha256}", tz = "America/New_York", time = "16:00", weekdays_before = 1, source = "synthetic test table" }}"#
    );
    format!(
        "{before}{}",
        after.replacen(section, &format!("{section}\n{line}"), 1)
    )
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
    std::fs::create_dir_all(dir.join("dividend")).unwrap();
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
fn every_dividend_charge_recomputes_from_the_record_and_the_cost_file() {
    let dir = tempfile::tempdir().unwrap();
    let table = full_table();
    let path = write_costs(dir.path(), &table);
    let (_, trials) = trials(&spec_with(path.clone()));
    assert_eq!(trials.len(), 1);
    let t = &trials[0];
    assert!(
        matches!(t.outcome, TrialOutcome::Ok { .. }),
        "{:?}",
        t.outcome
    );

    // The run block: the table by its configured path and sha256, and its instant.
    let listed = &t.run.dividend_tables["XAUUSD"];
    assert_eq!(listed.path, TABLE);
    assert_eq!(listed.sha256, sha256_hex(table.as_bytes()));
    assert_eq!(listed.tz, "America/New_York");
    assert_eq!(listed.weekdays_before, 1);
    assert_eq!(t.run.dividend_tables.len(), 1);
    assert!(
        t.run
            .estimates
            .iter()
            .any(|e| e.instrument == "XAUUSD" && e.component == CostComponent::Dividend)
    );
    assert!(t.run.is_costed("XAUUSD"));

    // Recompute every dividend charge from the record, the cost file and the table.
    let costs = CostConfig::from_path(&path).unwrap();
    let pip_size = costs.instrument("XAUUSD").unwrap().pip_size;
    let cost = &costs.venue("example_spread_bet").unwrap().instruments["XAUUSD"];
    let events = cost.dividend.as_ref().unwrap().events.as_ref().unwrap();
    let trades = t.trades().unwrap();
    let records = t
        .rollovers()
        .expect("a charged dividend writes the records");
    let dividends: Vec<_> = records.iter().filter(|r| r.dividend.is_some()).collect();
    assert!(dividends.len() >= 5, "only {} dividends", dividends.len());
    let (mut zero, mut bound) = (false, false);
    for r in &dividends {
        let trade = trades.iter().find(|x| x.id == r.position).unwrap();
        let part = r.dividend.as_ref().unwrap();
        let row = events.row(part.ex_date).unwrap();
        assert_eq!(&part.row, row);
        let (share, sign) = match trade.direction {
            Direction::Long => (row.long_share, -1.0),
            Direction::Short => (row.short_share, 1.0),
        };
        assert_eq!(part.share, share);
        let expect = sign * row.points * pip_size * share * trade.size.abs();
        assert!(
            (part.charge - expect).abs() < 1e-12,
            "{} vs {expect}",
            part.charge
        );
        assert_eq!(r.charge, part.charge);
        assert_eq!(r.days, 0);
        assert!(r.components.is_none());
        // Held across the instant: after the entry and before the exit.
        assert!(trade.entry_ts < r.at_utc && r.at_utc < trade.exit_ts);
        assert_eq!(r.direction, Some(trade.direction));
        assert_eq!(r.size, Some(trade.size));
        assert_eq!(r.fx_rate, Some(trade.fx_rate));
        assert_eq!(r.charge_account, Some(r.charge * 0.8));
        assert_eq!(part.charge_account, part.charge * 0.8);
        assert_eq!(part.table_path, TABLE);
        assert_eq!(part.table_sha256, sha256_hex(table.as_bytes()));
        zero |= row.points == 0.0;
        bound |= row.bound;
    }
    assert!(
        zero && bound,
        "no zero row ({zero}) or no bound row ({bound})"
    );
    // Each trade's financing is the sum of its records, and its dividend totals are the
    // sum of its credits and of its debits.
    for trade in trades {
        let mine = || records.iter().filter(|r| r.position == trade.id);
        let sum: f64 = mine().map(|r| r.charge).sum();
        assert!(
            (trade.financing - sum).abs() < 1e-9,
            "{} vs {sum}",
            trade.financing
        );
        let parts: Vec<f64> = mine()
            .filter_map(|r| r.dividend.as_ref().map(|d| d.charge))
            .collect();
        let credit: f64 = parts.iter().filter(|c| **c < 0.0).sum();
        let debit: f64 = parts.iter().filter(|c| **c >= 0.0).sum();
        let totals = trade.dividend.unwrap();
        assert!((totals.credit - credit).abs() < 1e-12);
        assert!((totals.debit - debit).abs() < 1e-12);
    }
}

#[test]
fn a_position_held_past_the_tables_last_date_fails_the_trial() {
    let dir = tempfile::tempdir().unwrap();
    let (_, full) = trials(&spec_with(write_costs(dir.path(), &full_table())));
    let charged = full[0]
        .rollovers()
        .unwrap()
        .iter()
        .find_map(|r| r.dividend.as_ref().map(|d| d.ex_date))
        .unwrap();
    // A table that ends on the day before the first charged ex-date.
    let short = tempfile::tempdir().unwrap();
    let last = charged.pred_opt().unwrap();
    let (_, trials) = trials(&spec_with(write_costs(
        short.path(),
        &synthetic_table(last),
    )));
    match &trials[0].outcome {
        TrialOutcome::Failed { stage, error } => {
            assert_eq!(*stage, FailureStage::Run);
            assert!(
                error.contains("dividend table covers 2024-01-01 to"),
                "{error}"
            );
        }
        other @ TrialOutcome::Ok { .. } => panic!("expected a failed trial, got {other:?}"),
    }
}

#[test]
fn an_altered_table_stops_the_sweep_before_any_trial() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_costs(dir.path(), &full_table());
    let altered = full_table().replacen(",0.9,", ",0.8,", 1);
    std::fs::write(dir.path().join(TABLE), altered).unwrap();
    let mut registry = JsonlRegistry::new(Vec::new());
    let err = run_sweep(&spec_with(path), &toy::registry(), &reader(), &mut registry).unwrap_err();
    assert!(err.to_string().contains("sha256"), "{err}");
    assert!(registry.into_inner().is_empty());
}

#[test]
fn a_sweep_without_a_dividend_writes_none_of_the_new_keys() {
    let (text, trials) = trials(&SweepSpec::from_toml_str(HEAD).unwrap());
    assert!(trials[0].rollovers().is_none());
    for key in ["\"dividend\"", "\"dividend_tables\"", "\"table_sha256\""] {
        assert!(!text.contains(key), "{key} in a builtin sweep's registry");
    }
}
