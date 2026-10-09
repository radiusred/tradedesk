//! The walk-forward on a synthetic sweep with hand-computed answers: the registered
//! nine folds over 2020-01-01 → 2026-02-27 (the ninth short), selection with a ruined
//! trial, an undefined Sharpe and a tie, the stitched series and £ curve, and trades
//! attributed to test windows at a fold boundary.

use chrono::{Datelike as _, Days, TimeZone as _, Weekday};

use super::*;
use crate::metrics::{CostBreakdown, PointKind};
use crate::{Direction, ExitReason, PositionId};

const CAPITAL: f64 = 100_000.0;
const NOISE: f64 = 0.01;
/// Costs booked per day on every curve: gross P&L = net P&L + £10 a day.
const DAILY_COST: f64 = 10.0;

fn d(s: &str) -> NaiveDate {
    s.parse().unwrap()
}

fn ts(s: &str) -> DateTime<Utc> {
    Utc.from_utc_datetime(&chrono::NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").unwrap())
}

/// The registered window.
fn window() -> (NaiveDate, NaiveDate) {
    (d("2020-01-01"), d("2026-02-27"))
}

/// The nine folds of the walk-forward Decision on #60, written out by hand.
fn decision_table() -> Vec<Fold> {
    [
        ("2020-01-01", "2021-12-31", "2022-01-01", "2022-06-30"),
        ("2020-07-01", "2022-06-30", "2022-07-01", "2022-12-31"),
        ("2021-01-01", "2022-12-31", "2023-01-01", "2023-06-30"),
        ("2021-07-01", "2023-06-30", "2023-07-01", "2023-12-31"),
        ("2022-01-01", "2023-12-31", "2024-01-01", "2024-06-30"),
        ("2022-07-01", "2024-06-30", "2024-07-01", "2024-12-31"),
        ("2023-01-01", "2024-12-31", "2025-01-01", "2025-06-30"),
        ("2023-07-01", "2025-06-30", "2025-07-01", "2025-12-31"),
        ("2024-01-01", "2025-12-31", "2026-01-01", "2026-02-27"),
    ]
    .iter()
    .enumerate()
    .map(|(i, (a, b, c, e))| Fold {
        fold: i + 1,
        train_first: d(a),
        train_last: d(b),
        test_first: d(c),
        test_last: d(e),
    })
    .collect()
}

fn weekdays(first: NaiveDate, last: NaiveDate) -> Vec<NaiveDate> {
    first
        .iter_days()
        .take_while(|x| *x <= last)
        .filter(|x| !matches!(x.weekday(), Weekday::Sat | Weekday::Sun))
        .collect()
}

/// The half-year a date falls in, from 2020H1 = 0.
fn half_year(date: NaiveDate) -> i32 {
    (date.year() - 2020) * 2 + i32::from(date.month() > 6)
}

/// The synthetic trials, by cell index. Every trial but the flat one adds the same
/// alternating ±1% noise to its drift, so two trials with a constant drift over a
/// window have the same std there and the higher drift has the higher Sharpe.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Kind {
    /// 0: no trade, every return 0, so every Sharpe is undefined.
    Flat,
    /// 1: the best drift of all, but ruined, so never selected.
    Ruined,
    /// 2: +0.12% a day throughout.
    Steady,
    /// 3: +0.2% a day until 2022H2, −0.2% from 2023H1.
    Early,
    /// 4: identical to `Steady`, so it ties with it and loses on the cell index.
    Twin,
}

const KINDS: [Kind; 5] = [
    Kind::Flat,
    Kind::Ruined,
    Kind::Steady,
    Kind::Early,
    Kind::Twin,
];

fn daily_return(kind: Kind, i: usize, date: NaiveDate) -> f64 {
    let noise = if i % 2 == 0 { NOISE } else { -NOISE };
    match kind {
        Kind::Flat => 0.0,
        Kind::Ruined => 0.01 + noise,
        Kind::Steady | Kind::Twin => 0.0012 + noise,
        Kind::Early if half_year(date) <= 5 => 0.002 + noise,
        Kind::Early => -0.002 + noise,
    }
}

/// A curve compounding `kind`'s returns from the capital, marked at each next midnight,
/// with £10 a day of costs between gross and net.
fn curve(kind: Kind, dates: &[NaiveDate]) -> Vec<EquityPoint> {
    let mut equity = CAPITAL;
    dates
        .iter()
        .enumerate()
        .map(|(i, &date)| {
            let r = daily_return(kind, i, date);
            equity *= 1.0 + r;
            #[allow(clippy::cast_precision_loss)]
            let costs = DAILY_COST * (i + 1) as f64;
            EquityPoint {
                date,
                kind: PointKind::Weekday,
                at: (date + Days::new(1))
                    .and_time(chrono::NaiveTime::MIN)
                    .and_utc(),
                equity,
                realised_net: 0.0,
                unrealised_net: equity - CAPITAL,
                open_positions: 0,
                open_exit_cost: 0.0,
                gross_pnl: equity - CAPITAL + costs,
                costs: CostBreakdown {
                    spread: costs,
                    ..CostBreakdown::default()
                },
                simple_return: Some(r),
                log_return: Some((1.0 + r).ln()),
            }
        })
        .collect()
}

/// A long round trip exiting at `exit`, with `gross` before a `spread` cost.
fn trade(exit: &str, gross: f64, spread: f64) -> ClosedTrade {
    let exit_ts = ts(exit);
    ClosedTrade {
        id: PositionId(0),
        instrument: "USA500IDXUSD".to_owned(),
        venue: "ig_spread_bet".to_owned(),
        quote_currency: "GBP".to_owned(),
        direction: Direction::Long,
        size: 1.0,
        entry_ts: exit_ts - chrono::Duration::days(3),
        exit_ts,
        bars_held: 3,
        entry_mark: 100.0,
        exit_mark: 100.0 + gross,
        entry_price: 100.0,
        exit_price: 100.0 + gross - spread,
        spread_cost: spread,
        slippage_cost: 0.0,
        financing: 0.0,
        financing_days: 0,
        commission: 0.0,
        gross_pnl_quote: gross,
        net_pnl_quote: gross - spread,
        fx_rate: 1.0,
        gross_pnl_account: gross,
        net_pnl_account: gross - spread,
        exit_reason: ExitReason::Signal,
        signal_reason: None,
    }
}

/// The trades of each trial. Only those exiting inside a selected trial's test window
/// count; the others carry large figures so a misattribution shows.
fn trades(kind: Kind) -> Option<Vec<ClosedTrade>> {
    match kind {
        Kind::Flat | Kind::Twin => Some(Vec::new()),
        // Not recorded (a record written before trades were).
        Kind::Ruined => None,
        Kind::Early => Some(vec![
            // Friday 2021-12-31's close: booked in fold 1's train window.
            trade("2022-01-01 00:00:00", 1.0e6, 1.0),
            // Fold 1's test window, where `Early` is selected.
            trade("2022-01-03 15:00:00", 100.0, 10.0),
            // Thursday 2022-06-30's close, fold 1's last test point.
            trade("2022-07-01 00:00:00", 50.0, 10.0),
            // Fold 4, where `Steady` is selected.
            trade("2023-07-03 12:00:00", 1.0e6, 1.0),
        ]),
        Kind::Steady => Some(vec![
            // Fold 1, where `Early` is selected.
            trade("2022-03-01 12:00:00", 1.0e6, 1.0),
            // Fold 5.
            trade("2024-02-01 12:00:00", 200.0, 20.0),
            // The window's close, inside fold 9.
            trade("2026-02-28 00:00:00", -30.0, 5.0),
        ]),
    }
}

struct Owned {
    kind: Kind,
    trial_id: String,
    params: BTreeMap<String, Value>,
    points: Vec<EquityPoint>,
    trades: Option<Vec<ClosedTrade>>,
}

fn owned(kinds: &[Kind]) -> Vec<Owned> {
    let (first, last) = window();
    let dates = weekdays(first, last);
    kinds
        .iter()
        .enumerate()
        .map(|(i, &kind)| Owned {
            kind,
            trial_id: format!("trial-{i}"),
            params: BTreeMap::from([("cell".to_owned(), Value::from(i))]),
            points: curve(kind, &dates),
            trades: trades(kind),
        })
        .collect()
}

fn views(trials: &[Owned]) -> Vec<View<'_>> {
    trials
        .iter()
        .enumerate()
        .map(|(index, t)| View {
            index,
            trial_id: &t.trial_id,
            params: &t.params,
            ruined: t.kind == Kind::Ruined,
            costed: true,
            capital: CAPITAL,
            risk_free_rate: 0.0,
            points: &t.points,
            trades: t.trades.as_deref(),
        })
        .collect()
}

fn run(trials: &[Owned]) -> WalkForwardReport {
    WalkForwardReport::compute(
        "sweep".to_owned(),
        trials.len() + 1, // one failed cell
        window(),
        &views(trials),
        WalkForwardOptions::default(),
    )
    .unwrap()
}

/// The returns `kind` has on the dates `first..=last`, by its own day numbering.
fn returns_on(kind: Kind, first: NaiveDate, last: NaiveDate) -> Vec<f64> {
    let (a, b) = window();
    weekdays(a, b)
        .into_iter()
        .enumerate()
        .filter(|(_, date)| (first..=last).contains(date))
        .map(|(i, date)| daily_return(kind, i, date))
        .collect()
}

#[test]
fn the_registered_window_has_the_nine_folds_of_the_decision() {
    let (first, last) = window();
    assert_eq!(
        folds(first, last, WalkForwardOptions::default()),
        decision_table()
    );
    // A window ending on a test boundary has no short fold.
    let eight = folds(first, d("2025-12-31"), WalkForwardOptions::default());
    assert_eq!(eight.len(), 8);
    assert_eq!(eight[7], decision_table()[7]);
    // Two years and a day: one fold, tested on that day.
    let one = folds(first, d("2022-01-01"), WalkForwardOptions::default());
    assert_eq!(one.len(), 1);
    assert_eq!(
        (one[0].test_first, one[0].test_last),
        (d("2022-01-01"), d("2022-01-01"))
    );
    assert!(folds(first, d("2021-12-31"), WalkForwardOptions::default()).is_empty());
    let zero = WalkForwardOptions {
        step_months: 0,
        ..WalkForwardOptions::default()
    };
    assert!(folds(first, last, zero).is_empty());
}

#[test]
fn each_fold_selects_the_best_train_sharpe_skipping_ruin_and_breaking_ties_low() {
    let trials = owned(&KINDS);
    let r = run(&trials);
    assert_eq!((r.trials, r.completed, r.failed, r.ruined), (6, 5, 1, 1));
    let table = decision_table();
    let counts = [129, 131, 130, 130, 130, 132, 129, 132, 42];
    for (i, f) in r.folds.iter().enumerate() {
        assert_eq!(f.fold, table[i]);
        assert_eq!(f.test_days, counts[i], "fold {}", i + 1);
        let s = f.selected.as_ref().unwrap();
        // `Early` wins while its drift is +0.2% over the whole train window (folds 1-3);
        // from fold 4 its train window holds a −0.2% half-year and `Steady` wins. `Twin`
        // ties `Steady` exactly and loses on the index; `Ruined` is better than both and
        // is never chosen; `Flat` has no Sharpe.
        let expected = if i < 3 { 3 } else { 2 };
        assert_eq!(s.index, expected, "fold {}", i + 1);
        assert_eq!(s.trial_id, format!("trial-{expected}"));
        let kind = KINDS[expected];
        let train = return_stats(
            &returns_on(kind, table[i].train_first, table[i].train_last),
            0.0,
        );
        let test = return_stats(
            &returns_on(kind, table[i].test_first, table[i].test_last),
            0.0,
        );
        assert_eq!(s.train_sharpe, train.sharpe);
        assert_eq!(s.test_sharpe, test.sharpe);
    }
    assert_eq!(r.folds[0].train_days, 523);
    let mean = |xs: Vec<f64>| xs.iter().sum::<f64>() / 9.0;
    let is = mean(
        r.folds
            .iter()
            .map(|f| f.selected.as_ref().unwrap().train_sharpe.unwrap())
            .collect(),
    );
    let oos = mean(
        r.folds
            .iter()
            .map(|f| f.selected.as_ref().unwrap().test_sharpe.unwrap())
            .collect(),
    );
    assert_eq!(r.mean_is_sharpe, Some(is));
    assert_eq!(r.mean_oos_sharpe, Some(oos));
}

#[test]
fn the_stitched_series_is_the_selected_trials_test_days_in_fold_order() {
    let trials = owned(&KINDS);
    let r = run(&trials);
    let s = r.stitched.as_ref().unwrap();
    let mut expected = Vec::new();
    for (i, f) in decision_table().iter().enumerate() {
        let kind = if i < 3 { Kind::Early } else { Kind::Steady };
        expected.extend(returns_on(kind, f.test_first, f.test_last));
    }
    assert_eq!(expected.len(), 1085);
    assert_eq!(
        (s.first_day, s.last_day),
        (d("2022-01-03"), d("2026-02-27"))
    );
    assert_eq!(s.returns, return_stats(&expected, 0.0));
    assert!(s.returns.sharpe.is_some());
    assert_eq!(s.days.len(), 1085);
    assert_eq!((s.days[0].fold, s.days[0].index), (1, 3));
    assert_eq!((s.days[1084].fold, s.days[1084].index), (9, 2));

    // The £ curve adds each selected trial's own daily £ change.
    let mut equity = CAPITAL;
    let mut dated = vec![(d("2022-01-02"), CAPITAL)];
    for (i, f) in decision_table().iter().enumerate() {
        let t = &trials[if i < 3 { 3 } else { 2 }];
        for (j, p) in t.points.iter().enumerate() {
            if (f.test_first..=f.test_last).contains(&p.date) {
                equity += p.equity - t.points[j - 1].equity;
                dated.push((p.date, equity));
            }
        }
    }
    assert!((s.final_equity - equity).abs() < 1e-6);
    assert!((s.pnl - (equity - CAPITAL)).abs() < 1e-6);
    let dd = max_drawdown(&dated).by_amount.unwrap();
    let got = s.max_drawdown.by_amount.as_ref().unwrap();
    assert!((got.amount - dd.amount).abs() < 1e-6);
    assert_eq!((got.peak, got.trough), (dd.peak, dd.trough));
}

#[test]
fn oos_trades_are_those_booked_in_a_selected_trials_test_window() {
    let r = run(&owned(&KINDS));
    let per_fold: Vec<Option<usize>> = r
        .folds
        .iter()
        .map(|f| f.selected.as_ref().unwrap().test_trades)
        .collect();
    // Fold 1: `Early`'s 2022-01-03 and 2022-06-30-close exits, not its 2021-12-31-close
    // one. Folds 5 and 9: `Steady`'s. `Early`'s fold-4 exit and `Steady`'s fold-1 exit
    // are in folds that selected the other trial.
    assert_eq!(per_fold, [2, 0, 0, 0, 1, 0, 0, 0, 1].map(Some));
    let t = r.stitched.unwrap().trades.unwrap();
    // `Early` 100 and 50 in fold 1; `Steady` 200 in fold 5 and −30 in fold 9.
    assert_eq!(t.stats.count, 4);
    assert!((t.stats.gross_pnl - 320.0).abs() < 1e-9);
    assert!((t.stats.costs.total() - 45.0).abs() < 1e-9);
    assert!((t.stats.net_pnl - 275.0).abs() < 1e-9);
    assert!((t.cost_drag.unwrap() - 45.0 / 320.0).abs() < 1e-12);
}

#[test]
fn every_cell_is_reported_over_the_same_oos_days() {
    let trials = owned(&KINDS);
    let r = run(&trials);
    assert_eq!(r.cells.len(), 5);
    let (first, last) = (d("2022-01-01"), d("2026-02-27"));
    for (c, t) in r.cells.iter().zip(&trials) {
        assert_eq!(c.observations, 1085);
        let start = t.points.iter().position(|p| p.date >= first).unwrap();
        let net = t.points[1607].equity - t.points[start - 1].equity;
        assert!((c.net_pnl - net).abs() < 1e-6, "cell {}", c.index);
        assert!((c.gross_pnl - (net + DAILY_COST * 1085.0)).abs() < 1e-6);
    }
    assert_eq!(r.cells[0].sharpe, None, "a flat cell has no Sharpe");
    assert!(r.cells[1].ruined);
    assert_eq!(r.cells[1].sharpe, None, "a ruined cell has no Sharpe");
    assert_eq!(
        r.cells[2].sharpe,
        return_stats(&returns_on(Kind::Steady, first, last), 0.0).sharpe
    );
    assert_eq!(r.cells[2].sharpe, r.cells[4].sharpe);
    let counts: Vec<Option<usize>> = r.cells.iter().map(|c| c.trades).collect();
    assert_eq!(counts, [Some(0), None, Some(3), Some(3), Some(0)]);
}

#[test]
fn a_selected_record_without_trades_leaves_the_oos_trades_undefined() {
    let mut trials = owned(&[Kind::Flat, Kind::Steady]);
    trials[1].trades = None;
    let r = run(&trials);
    let s = r.stitched.unwrap();
    assert!(s.trades.is_none());
    assert!(
        r.folds
            .iter()
            .all(|f| f.selected.as_ref().unwrap().index == 1)
    );
}

#[test]
fn undefined_sharpes_rank_last_and_tie_to_the_lowest_index() {
    // Only flat cells: every train Sharpe is undefined, so the lowest index is chosen.
    let r = run(&owned(&[Kind::Flat, Kind::Flat]));
    assert!(
        r.folds
            .iter()
            .all(|f| f.selected.as_ref().unwrap().index == 0)
    );
    assert_eq!((r.mean_is_sharpe, r.mean_oos_sharpe), (None, None));
    let s = r.stitched.unwrap();
    assert_eq!(s.returns.sharpe, None);
    assert!(s.max_drawdown.by_amount.is_none());
}

#[test]
fn a_sweep_of_ruined_trials_selects_nothing_and_stitches_nothing() {
    let r = run(&owned(&[Kind::Ruined]));
    assert!(r.folds.iter().all(|f| f.selected.is_none()));
    assert!(r.stitched.is_none());
    assert_eq!(r.cells.len(), 1);
}

#[test]
fn refused_inputs() {
    let trials = owned(&[Kind::Steady, Kind::Twin]);
    let views = views(&trials);
    let bad = WalkForwardOptions {
        step_months: 3,
        ..WalkForwardOptions::default()
    };
    assert!(matches!(
        WalkForwardReport::compute("s".into(), 2, window(), &views, bad),
        Err(WalkForwardError::InvalidOptions { .. })
    ));
    assert_eq!(
        WalkForwardReport::compute(
            "s".into(),
            2,
            (d("2020-01-01"), d("2021-06-30")),
            &views,
            WalkForwardOptions::default()
        ),
        Err(WalkForwardError::NoFold {
            first: d("2020-01-01"),
            last: d("2021-06-30")
        })
    );
    assert_eq!(
        WalkForwardReport::compute("s".into(), 1, window(), &[], WalkForwardOptions::default()),
        Err(WalkForwardError::NoCompletedTrial)
    );
    let mut short = owned(&[Kind::Steady, Kind::Twin]);
    short[1].points.pop();
    assert!(matches!(
        WalkForwardReport::compute(
            "s".into(),
            2,
            window(),
            &super::tests::views(&short),
            WalkForwardOptions::default()
        ),
        Err(WalkForwardError::Sweep(ReportError::MisalignedDates { .. }))
    ));
}
