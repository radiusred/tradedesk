//! A tiny sweep on synthetic bars: the data is loaded once, the registry is the same
//! on a second run, failed cells are recorded while the others complete, the registry
//! round-trips through its JSON Lines file, and the report runs over it. The strategy is
//! the test strategy (`toy`).
#![allow(clippy::float_cmp)]

mod synthetic;
mod toy;

use std::collections::BTreeMap;

use chrono::{DateTime, Utc};
use synthetic::{MemReader, breakout_path, choppy_days, first_day};
use tradedesk_backtest::sweep::report::PboOutcome;
use tradedesk_backtest::sweep::{
    FailureStage, JsonlRegistry, RegistryRecord, RegistrySink, ReportOptions, StrategyRegistry,
    SweepError, SweepReport, SweepSpec, TrialOutcome, read_registry, run_sweep,
    run_sweep_cancellable,
};
use tradedesk_backtest::{EndOfRun, EndOfRunRule};
use tradedesk_data::Side;

/// A grid over the choppy walk: two instruments × (a $50 stop or the invalid 0) × two
/// targets, entering on a cross of $2060 and exiting under $2040.
const SPEC: &str = r#"
name = "synthetic threshold"
strategy = "threshold_cross"
base = "default"
instruments = ["XAUUSD", "EURUSD"]
first_day = "2024-01-01"
last_day = "2024-05-02"
venue = "ig_spread_bet"
exit_evaluation = { mode = "intrabar", both_hit = "stop_first" }
account_fx = { account_currency = "GBP", rates = { USD = 0.8 } }
sizing = { kind = "stake_per_point", stake = 0.01 }
end_of_run = "close"
starting_capital = 25000.0

[grid]
enter_above = [206000.0]
exit_below = [204000.0]
stop_distance = [5000.0, 0.0]
target_distance = [3000.0, 8000.0]
"#;

/// The breakout, then nine weeks of the choppy walk, so the cells trade often and
/// differently.
fn reader() -> MemReader {
    let days = breakout_path(&choppy_days(63));
    MemReader::default()
        .with_days("XAUUSD", first_day(), &days)
        .with_days("EURUSD", first_day(), &days)
}

fn spec() -> SweepSpec {
    SweepSpec::from_toml_str(SPEC).unwrap()
}

fn strategies() -> StrategyRegistry {
    toy::registry()
}

/// Every timestamp set to the epoch, for comparing two runs.
fn without_clock(mut records: Vec<RegistryRecord>) -> Vec<RegistryRecord> {
    let epoch = DateTime::<Utc>::UNIX_EPOCH;
    for r in &mut records {
        match r {
            RegistryRecord::Sweep(s) => s.started_utc = epoch,
            RegistryRecord::Trial(t) => {
                t.started_utc = epoch;
                t.finished_utc = epoch;
            }
        }
    }
    records
}

fn trials(records: &[RegistryRecord]) -> Vec<&tradedesk_backtest::sweep::TrialRecord> {
    records
        .iter()
        .filter_map(|r| match r {
            RegistryRecord::Trial(t) => Some(t.as_ref()),
            RegistryRecord::Sweep(_) => None,
        })
        .collect()
}

#[test]
fn the_data_is_loaded_once_and_failed_cells_are_recorded() {
    let reader = reader();
    let mut records = Vec::new();
    let summary = run_sweep(&spec(), &strategies(), &reader, &mut records).unwrap();
    assert_eq!((summary.cells, summary.ok, summary.failed), (8, 4, 4));
    // Eight cells, two instruments: each (symbol, side) was read exactly once.
    let reads = reader.reads();
    assert_eq!(reads.len(), 4);
    for symbol in ["XAUUSD", "EURUSD"] {
        for side in [Side::Bid, Side::Ask] {
            assert_eq!(reads[&(symbol.to_owned(), side)], 1, "{symbol} {side:?}");
        }
    }
    // The header first, then the trials in cell order.
    assert!(matches!(&records[0], RegistryRecord::Sweep(s) if s.cells == 8));
    let trials = trials(&records);
    assert_eq!(
        trials.iter().map(|t| t.index).collect::<Vec<_>>(),
        (0..8).collect::<Vec<_>>()
    );
    for t in &trials {
        let stop = t.params["stop_distance"].as_f64().unwrap();
        match &t.outcome {
            TrialOutcome::Failed { stage, error } => {
                assert_eq!(stop, 0.0);
                assert_eq!(*stage, FailureStage::Config);
                assert!(error.contains("stop_distance"), "{error}");
                assert!(t.config.is_none());
            }
            TrialOutcome::Ok {
                engine,
                metrics,
                trades,
            } => {
                assert_eq!(stop, 5000.0);
                assert!(engine.entries > 0, "cell {} never traded", t.index);
                // Every closed trade is recorded (#62), the ones `TradeStats` counts.
                let trades = trades.as_ref().expect("trades recorded");
                assert_eq!(trades.len(), metrics.trades.count);
                let net: f64 = trades.iter().map(|x| x.net_pnl_account).sum();
                assert!((net - metrics.trades.net_pnl).abs() < 1e-6);
                assert_eq!(metrics.run.venue, "ig_spread_bet");
                // Task #58: no builtin placeholder; the trial is costed.
                assert!(metrics.run.placeholders.is_empty());
                assert!(metrics.run.is_costed(&t.instrument), "{}", t.instrument);
                assert_eq!(t.run, metrics.run);
                // The end-of-run rules are in the trial record (#59).
                assert_eq!(t.run.end_of_run, Some(EndOfRunRule::new(EndOfRun::Close)));
                assert_eq!(t.config.as_ref().unwrap()["stop_distance"], 5000.0);
            }
        }
    }
    // The two target cells really differ.
    let pnl = |i: usize| trials[i].metrics().unwrap().final_equity;
    assert_ne!(pnl(0), pnl(1));
}

#[test]
fn a_second_run_writes_the_same_registry() {
    let mut a = Vec::new();
    let mut b = Vec::new();
    run_sweep(&spec(), &strategies(), &reader(), &mut a).unwrap();
    run_sweep(&spec(), &strategies(), &reader(), &mut b).unwrap();
    let ids: Vec<&str> = trials(&a).iter().map(|t| t.trial_id.as_str()).collect();
    let unique: std::collections::BTreeSet<_> = ids.iter().collect();
    assert_eq!(unique.len(), ids.len(), "trial ids are distinct");
    assert_eq!(without_clock(a), without_clock(b));
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
fn the_registry_file_reads_back_losslessly() {
    let path = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join("sweep_registry.jsonl");
    let _ = std::fs::remove_file(&path);
    let mut memory = Vec::new();
    {
        let mut file = JsonlRegistry::append(&path).unwrap();
        run_sweep(
            &spec(),
            &strategies(),
            &reader(),
            &mut Tee(&mut memory, &mut file),
        )
        .unwrap();
    }
    let loaded = read_registry(&path).unwrap();
    assert_eq!(loaded, memory);
    // A trial written before trades were recorded (#62) reads back with none.
    let text = std::fs::read_to_string(&path).unwrap();
    let mut old: serde_json::Value = serde_json::from_str(text.lines().nth(1).unwrap()).unwrap();
    assert!(old["outcome"]["trades"].is_array());
    old["outcome"].as_object_mut().unwrap().remove("trades");
    let old = tradedesk_backtest::sweep::parse_registry(format!("{old}\n").as_bytes()).unwrap();
    let RegistryRecord::Trial(old) = &old[0] else {
        panic!("a trial");
    };
    assert!(old.trades().is_none());
    assert!(old.metrics().is_some());
    // Appending a second run keeps the first; the report counts each trial once.
    {
        let mut file = JsonlRegistry::append(&path).unwrap();
        run_sweep(&spec(), &strategies(), &reader(), &mut file).unwrap();
    }
    let twice = read_registry(&path).unwrap();
    assert_eq!(twice.len(), 2 * memory.len());
    let once = SweepReport::from_records(&memory, None, ReportOptions::default()).unwrap();
    let again = SweepReport::from_records(&twice, None, ReportOptions::default()).unwrap();
    assert_eq!(once, again);
}

#[test]
fn the_report_deflates_the_best_trial_against_the_sweep() {
    let mut records = Vec::new();
    run_sweep(&spec(), &strategies(), &reader(), &mut records).unwrap();
    let report = SweepReport::from_records(&records, None, ReportOptions::default()).unwrap();
    assert_eq!((report.trials, report.completed, report.failed), (8, 4, 4));
    assert_eq!(report.ruined, 0);
    assert_eq!(report.dsr.trials, 4);
    let best = report.best.as_ref().expect("a defined Sharpe");
    assert!(best.benchmark > 0.0);
    assert!((best.deflated_sharpe - (best.sharpe - best.benchmark)).abs() < 1e-12);
    assert!((0.0..=1.0).contains(&best.dsr));
    assert!((best.false_discovery_probability + best.dsr - 1.0).abs() < 1e-15);
    for t in &report.per_trial {
        if let Some(dsr) = t.dsr {
            assert!(
                dsr <= best.dsr + 1e-12,
                "the best trial has the highest DSR here"
            );
        }
    }
    // More prior trials raise the bar.
    let stricter = SweepReport::from_records(
        &records,
        None,
        ReportOptions {
            prior_trials: 100,
            ..ReportOptions::default()
        },
    )
    .unwrap();
    assert_eq!(stricter.dsr.trials, 104);
    assert!(stricter.best.unwrap().dsr <= best.dsr);
    match &report.pbo {
        PboOutcome::Computed(p) => {
            assert!((0.0..=1.0).contains(&p.pbo));
            assert_eq!(p.trials, 4);
            assert_eq!(p.cscv.blocks, 16);
        }
        PboOutcome::NotComputed { reason } => panic!("PBO not computed: {reason}"),
    }
    // An unknown sweep id is refused.
    assert!(SweepReport::from_records(&records, Some("nope"), ReportOptions::default()).is_err());
    let _: BTreeMap<String, serde_json::Value> = best.trial.params.clone();
}

/// Sets the cancel flag as soon as it has written `after` records.
struct CancelAfter<'a> {
    records: Vec<RegistryRecord>,
    after: usize,
    cancel: &'a std::sync::atomic::AtomicBool,
}

impl RegistrySink for CancelAfter<'_> {
    fn write(&mut self, record: &RegistryRecord) -> std::io::Result<()> {
        self.records.push(record.clone());
        if self.records.len() >= self.after {
            self.cancel.store(true, std::sync::atomic::Ordering::SeqCst);
        }
        Ok(())
    }
}

#[test]
fn a_cancelled_sweep_writes_whole_records_and_says_so() {
    use std::sync::atomic::AtomicBool;
    // Cancelled before anything: nothing is written.
    let cancel = AtomicBool::new(true);
    let mut sink = CancelAfter {
        records: vec![],
        after: usize::MAX,
        cancel: &cancel,
    };
    let err =
        run_sweep_cancellable(&spec(), &strategies(), &reader(), &mut sink, &cancel).unwrap_err();
    assert!(matches!(err, SweepError::Cancelled { written: 0 }), "{err}");
    assert!(sink.records.is_empty());
    // Cancelled once the header is written (SIGINT during the sweep): no cell starts.
    let cancel = AtomicBool::new(false);
    let mut sink = CancelAfter {
        records: vec![],
        after: 1,
        cancel: &cancel,
    };
    let err =
        run_sweep_cancellable(&spec(), &strategies(), &reader(), &mut sink, &cancel).unwrap_err();
    assert!(matches!(err, SweepError::Cancelled { written: 0 }), "{err}");
    assert_eq!(sink.records.len(), 1);
    assert!(matches!(sink.records[0], RegistryRecord::Sweep(_)));
    // Never set: the same as run_sweep.
    let cancel = AtomicBool::new(false);
    let mut records = Vec::new();
    let summary =
        run_sweep_cancellable(&spec(), &strategies(), &reader(), &mut records, &cancel).unwrap();
    assert_eq!((summary.cells, summary.ok, summary.failed), (8, 4, 4));
}

#[test]
fn a_trial_on_a_placeholder_figure_is_not_costed_and_neither_is_its_sweep() {
    // The builtin config with IG's EURUSD spread turned back into a placeholder.
    let text = tradedesk_backtest::cost::config::BUILTIN_COSTS_TOML.replacen(
        "spread = { pips = 1.04, basis = \"average\", placeholder = false",
        "spread = { pips = 1.04, basis = \"average\", placeholder = true",
        1,
    );
    let path = std::path::Path::new(env!("CARGO_TARGET_TMPDIR")).join("costs_placeholder.toml");
    std::fs::write(&path, text).unwrap();
    let mut with_placeholder = spec();
    with_placeholder.cost_config = Some(path);
    let mut records = Vec::new();
    run_sweep(&with_placeholder, &strategies(), &reader(), &mut records).unwrap();
    let report = SweepReport::from_records(&records, None, ReportOptions::default()).unwrap();
    assert_eq!(report.completed, 4);
    for t in &report.per_trial {
        assert_eq!(t.costed, t.instrument == "XAUUSD", "{}", t.instrument);
    }
    assert!(!report.costed);
    // The builtin config costs both instruments.
    let mut records = Vec::new();
    run_sweep(&spec(), &strategies(), &reader(), &mut records).unwrap();
    let report = SweepReport::from_records(&records, None, ReportOptions::default()).unwrap();
    assert!(report.costed && report.per_trial.iter().all(|t| t.costed));
}
