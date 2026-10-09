//! Metrics over hand-built ledgers on synthetic hourly USA500 bars at IG (spread 0.4
//! points, slippage 0.1 a fill, no commission, 3.4% / 360 financing, rollover 22:00Z in
//! January), USD → GBP at 0.8.
#![allow(clippy::many_single_char_names)]

use super::*;
use crate::{
    AccountFx, BarTimeframe, BothHit, Direction, ExitEvaluation, ExitReason, FillAt, JoinedBar,
    JoinedSeries, Ohlcv, OpenOrder, PricePoint,
};
use chrono::{Datelike, Duration, TimeZone, Weekday};
use proptest::prelude::*;

const CAPITAL: f64 = 25_000.0;
const FIN: f64 = 0.034 / 360.0;

fn utc(y: i32, mo: u32, d: u32, h: u32) -> DateTime<Utc> {
    Utc.with_ymd_and_hms(y, mo, d, h, 0, 0).unwrap()
}

fn date(y: i32, mo: u32, d: u32) -> NaiveDate {
    NaiveDate::from_ymd_opt(y, mo, d).unwrap()
}

fn flat(p: f64) -> Ohlcv {
    Ohlcv {
        open: p,
        high: p,
        low: p,
        close: p,
        tick_volume: 1.0,
    }
}

fn bar(ts: DateTime<Utc>, mid: f64) -> JoinedBar {
    JoinedBar {
        ts_open_utc: ts,
        bid: flat(mid - 0.25),
        ask: flat(mid + 0.25),
    }
}

/// Hourly USA500 bars from `from`, mid `4800 + h` at hour `h`.
fn rising(from: DateTime<Utc>, hours: i64) -> JoinedSeries {
    #[allow(clippy::cast_precision_loss)]
    let bars = (0..hours)
        .map(|h| bar(from + Duration::hours(h), 4800.0 + h as f64))
        .collect();
    JoinedSeries::new("USA500IDXUSD", BarTimeframe::H1, bars).unwrap()
}

fn ledger() -> Ledger {
    Ledger::new(
        &crate::cost::config::arithmetic_fixture(),
        "ig_spread_bet",
        ExitEvaluation::Intrabar {
            both_hit: BothHit::StopFirst,
        },
        AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap(),
    )
    .unwrap()
}

fn order(direction: Direction, size: f64) -> OpenOrder {
    OpenOrder {
        instrument: "USA500IDXUSD".to_owned(),
        direction,
        size,
        stop: None,
        target: None,
    }
}

fn at_open(s: &JoinedSeries, i: usize) -> FillAt<'_> {
    FillAt {
        bar: &s.bars()[i],
        point: PricePoint::Open,
        ts: s.bars()[i].ts_open_utc,
        bar_index: i,
    }
}

fn close_to(a: f64, b: f64) -> bool {
    (a - b).abs() < 1e-9 * a.abs().max(b.abs()).max(1.0)
}

/// `equity = capital + gross − costs − open exit cost` on every point.
fn assert_identities(m: &Metrics) {
    for p in &m.equity {
        assert!(
            close_to(
                p.equity,
                m.starting_capital + p.gross_pnl - p.costs.total() - p.open_exit_cost
            ),
            "{p:?}"
        );
        assert!(close_to(
            p.equity,
            m.starting_capital + p.realised_net + p.unrealised_net
        ));
    }
    let t = &m.trades;
    assert!(close_to(t.gross_pnl - t.costs.total(), t.net_pnl));
}

/// Long 10 from the 10:00 open (mid 4810) to the 15:00 open (mid 4815) on Tuesday.
#[test]
fn one_winning_trade() {
    let s = rising(utc(2024, 1, 16, 0), 24);
    let mut l = ledger();
    let id = l
        .open(&order(Direction::Long, 10.0), at_open(&s, 10))
        .unwrap();
    l.close(id, at_open(&s, 15), ExitReason::Signal, &s)
        .unwrap();
    let cfg = MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 16), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    // Fills 4810.3 and 4814.7: net 44 USD = 35.2 GBP; gross 50 USD = 40 GBP.
    assert_eq!(m.equity.len(), 1);
    let p = &m.equity[0];
    assert_eq!((p.date, p.at), (date(2024, 1, 16), utc(2024, 1, 17, 0)));
    assert!(close_to(p.equity, CAPITAL + 35.2));
    assert!(close_to(p.realised_net, 35.2));
    assert_eq!(
        (p.unrealised_net, p.open_positions, p.open_exit_cost),
        (0.0, 0, 0.0)
    );
    assert!(close_to(p.gross_pnl, 40.0));
    assert!(close_to(p.costs.spread, 0.2 * 10.0 * 2.0 * 0.8));
    assert!(close_to(p.costs.slippage, 0.1 * 10.0 * 2.0 * 0.8));
    assert_eq!((p.costs.commission, p.costs.financing), (0.0, 0.0));
    assert!(close_to(p.simple_return.unwrap(), 35.2 / CAPITAL));
    assert!(close_to(p.log_return.unwrap(), (1.0 + 35.2 / CAPITAL).ln()));

    let t = &m.trades;
    assert_eq!((t.count, t.wins, t.losses, t.breakevens), (1, 1, 0, 0));
    assert_eq!(t.win_rate, Some(1.0));
    assert!(close_to(t.average_win.unwrap(), 35.2));
    assert_eq!(t.average_loss, None);
    assert!(close_to(t.expectancy.unwrap(), 35.2));
    assert!(close_to(t.gross_expectancy.unwrap(), 40.0));
    assert_eq!(t.profit_factor, None);
    assert_eq!((t.average_bars_held, t.max_bars_held), (Some(5.0), Some(5)));
    assert!(close_to(t.max_days_held.unwrap(), 5.0 / 24.0));
    assert_eq!((t.longest_winning_streak, t.longest_losing_streak), (1, 0));
    assert_eq!(t.by_exit_reason.signal.count, 1);
    assert!(close_to(t.by_exit_reason.signal.net_pnl, 35.2));
    assert_eq!(t.by_exit_reason.stop, ReasonStats::default());

    assert_eq!(m.returns.observations, 1);
    assert_eq!(m.returns.sharpe, None);
    assert_eq!(
        m.max_drawdown,
        MaxDrawdown {
            by_amount: None,
            by_fraction: None
        }
    );
    assert!(close_to(m.final_equity, CAPITAL + 35.2));
    assert!(close_to(m.total_return, 35.2 / CAPITAL));
    assert_eq!(m.open_at_end.count, 0);
    assert_eq!(m.ruined_on, None);
    assert_identities(&m);
}

/// Short 10 from the 10:00 open, stopped at the 15:00 open as the price rises.
#[test]
fn one_losing_trade() {
    let s = rising(utc(2024, 1, 16, 0), 24);
    let mut l = ledger();
    let id = l
        .open(&order(Direction::Short, 10.0), at_open(&s, 10))
        .unwrap();
    l.close(id, at_open(&s, 15), ExitReason::Stop, &s).unwrap();
    let cfg = MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 16), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    // Sold at 4809.7, bought back at 4815.3: net -56 USD = -44.8 GBP.
    assert!(close_to(m.final_equity, CAPITAL - 44.8));
    let t = &m.trades;
    assert_eq!((t.wins, t.losses), (0, 1));
    assert_eq!(t.win_rate, Some(0.0));
    assert!(close_to(t.average_loss.unwrap(), -44.8));
    assert_eq!(t.profit_factor, Some(0.0));
    assert_eq!(t.longest_losing_streak, 1);
    assert_eq!(t.by_exit_reason.stop.count, 1);
    assert!(close_to(t.gross_pnl, -40.0));

    // The pound drawdown is first-class; the opening equity is dated the day before.
    let dd = m.max_drawdown.by_amount.clone().unwrap();
    assert!(close_to(dd.amount, -44.8));
    assert!(close_to(dd.fraction, -44.8 / CAPITAL));
    assert_eq!(
        (dd.peak, dd.trough, dd.recovery),
        (date(2024, 1, 15), date(2024, 1, 16), None)
    );
    assert_eq!(m.max_drawdown.by_fraction, Some(dd));
    assert_identities(&m);
}

/// Long 2 from Tuesday 12:00 to Thursday 12:00, across the Tuesday and Wednesday 22:00Z
/// rollovers, marked each night before and after it closes.
#[test]
fn overnight_hold_with_financing() {
    let from = utc(2024, 1, 16, 12);
    let s = rising(from, 72);
    let mut l = ledger();
    let id = l
        .open(&order(Direction::Long, 2.0), at_open(&s, 0))
        .unwrap();
    // The ledger as it stood at Wednesday's close, for the agreement check below.
    let mut at_wed = l.clone();
    let t = l
        .close(id, at_open(&s, 48), ExitReason::Signal, &s)
        .unwrap()
        .clone();
    let cfg = MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 19), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();
    assert_eq!(m.equity.len(), 4);

    // Tuesday's close: marked at the 23:00 bar (mid 4811); the 22:00 rollover is charged
    // at the 21:00 bar's mid 4809. Liquidation sells at 4811 - 0.3.
    let fin_tue = 2.0 * 4809.0 * FIN;
    let tue = &m.equity[0];
    assert_eq!(tue.open_positions, 1);
    assert!(close_to(
        tue.unrealised_net,
        0.8 * (2.0 * (4810.7 - 4800.3) - fin_tue)
    ));
    assert!(close_to(tue.open_exit_cost, 0.8 * 2.0 * 0.3));
    assert!(close_to(tue.costs.financing, 0.8 * fin_tue));
    assert!(close_to(tue.costs.spread, 0.8 * 2.0 * 0.2));
    assert!(close_to(tue.gross_pnl, 0.8 * 2.0 * 11.0));

    // Wednesday's close agrees with the ledger's own mid mark after the same accrual.
    let wed_close = utc(2024, 1, 18, 0);
    at_wed.accrue_financing(wed_close, &s).unwrap();
    let mid = at_wed.mark_to_market(wed_close, &s).unwrap();
    let wed = &m.equity[1];
    assert!(close_to(
        wed.unrealised_net + wed.open_exit_cost,
        mid[0].unrealised_net_account
    ));
    let fin_wed = 2.0 * 4833.0 * FIN;
    assert!(close_to(wed.costs.financing, 0.8 * (fin_tue + fin_wed)));

    // Thursday: closed at noon, realised; Friday carries it with a zero return.
    let (thu, fri) = (&m.equity[2], &m.equity[3]);
    assert_eq!((thu.open_positions, thu.unrealised_net), (0, 0.0));
    assert!(close_to(thu.equity, CAPITAL + t.net_pnl_account));
    assert!(close_to(thu.costs.financing, 0.8 * t.financing));
    assert_eq!(fri.equity, thu.equity);
    assert_eq!(fri.simple_return, Some(0.0));
    assert!(close_to(
        m.trades.costs.financing,
        0.8 * (fin_tue + fin_wed)
    ));
    assert!(close_to(m.trades.average_days_held.unwrap(), 2.0));

    // Four daily returns: the Sharpe is defined and the count travels with it.
    assert_eq!(m.returns.observations, 4);
    assert!(m.returns.sharpe.is_some());
    assert_eq!(m.daily_returns().unwrap().len(), 4);
    assert_identities(&m);
}

/// Long 2 from Wednesday 12:00, still open when the window closes on Thursday.
#[test]
fn open_position_at_end_of_run() {
    let s = rising(utc(2024, 1, 16, 12), 72);
    let mut l = ledger();
    l.open(&order(Direction::Long, 2.0), at_open(&s, 24))
        .unwrap();
    let cfg = MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 18), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    // Not a closed trade, reported on its own.
    assert_eq!(m.trades.count, 0);
    assert_eq!(m.trades.expectancy, None);
    let end = &m.open_at_end;
    assert_eq!(end.count, 1);
    let v = &end.positions[0];
    // The clone's accrual charged Wednesday (mark 4833) and Thursday (mark 4857).
    assert!(close_to(v.financing, 0.8 * 2.0 * (4833.0 + 4857.0) * FIN));
    // The caller's ledger is untouched.
    assert!(l.financing_charges().is_empty());
    assert_eq!(v.mark_bar_open, utc(2024, 1, 18, 23));
    let last = m.equity.last().unwrap();
    assert!(close_to(last.unrealised_net, end.unrealised_net));
    assert!(close_to(last.open_exit_cost, end.exit_cost));
    // Tuesday's close is before the entry.
    assert_eq!(m.equity[0].open_positions, 0);
    assert_eq!(m.equity[0].equity, CAPITAL);

    // Liquidation value = what closing at that bar's close books.
    let window_close = utc(2024, 1, 19, 0);
    let mut closed = l.clone();
    let bar = &s.bars()[59];
    let t = closed
        .close(
            l.open_positions()[0].id,
            FillAt {
                bar,
                point: PricePoint::Close,
                ts: window_close,
                bar_index: 59,
            },
            ExitReason::EndOfRun,
            &s,
        )
        .unwrap();
    assert!(close_to(t.net_pnl_account, end.unrealised_net));
    // And the mid mark is the liquidation value plus the exit cost.
    let mut mid = l.clone();
    mid.accrue_financing(window_close, &s).unwrap();
    let mark = mid.mark_to_market(window_close, &s).unwrap();
    assert!(close_to(
        mark[0].unrealised_net_account,
        end.unrealised_net + end.exit_cost
    ));
    assert_identities(&m);
}

/// Weekends are not points: a Sunday-evening exit lands in Monday's point.
#[test]
fn weekends_are_omitted_and_monday_absorbs_them() {
    let mut bars: Vec<JoinedBar> = (0..10)
        .map(|h| bar(utc(2024, 1, 19, 12 + h), 4800.0))
        .collect();
    bars.push(bar(utc(2024, 1, 21, 23), 4850.0));
    bars.extend((0..12).map(|h| bar(utc(2024, 1, 22, h), 4850.0)));
    let s = JoinedSeries::new("USA500IDXUSD", BarTimeframe::H1, bars).unwrap();
    let mut l = ledger();
    let id = l
        .open(&order(Direction::Long, 1.0), at_open(&s, 0))
        .unwrap();
    let t = l
        .close(id, at_open(&s, 10), ExitReason::Signal, &s)
        .unwrap()
        .clone();
    let cfg = MetricsConfig::new(date(2024, 1, 19), date(2024, 1, 22), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    let dates: Vec<NaiveDate> = m.equity.iter().map(|p| p.date).collect();
    assert_eq!(dates, [date(2024, 1, 19), date(2024, 1, 22)]);
    // Friday: open, marked at the 21:00 bar, Friday's triple-day rollover charged.
    let fin = 4800.0 * FIN * 3.0;
    assert_eq!(t.financing_days, 3);
    let fri = &m.equity[0];
    assert_eq!(fri.open_positions, 1);
    assert!(close_to(
        fri.unrealised_net,
        0.8 * ((4799.7 - 4800.3) - fin)
    ));
    // Monday: the Sunday exit is realised, and the return runs Friday to Monday.
    let mon = &m.equity[1];
    assert!(close_to(mon.equity, CAPITAL + t.net_pnl_account));
    assert!(close_to(
        mon.simple_return.unwrap(),
        mon.equity / fri.equity - 1.0
    ));
    assert_eq!(m.returns.observations, 2);
    assert_identities(&m);
}

/// Hourly bars at mid `4800 + i` for the `i`-th bar: every hour from Monday 2024-03-25
/// to Friday 2024-03-29, then one bar opening at each of `weekend`.
fn week_of_25_march(weekend: &[DateTime<Utc>]) -> JoinedSeries {
    let mut opens: Vec<DateTime<Utc>> = (0..120)
        .map(|h| utc(2024, 3, 25, 0) + Duration::hours(h))
        .collect();
    opens.extend_from_slice(weekend);
    #[allow(clippy::cast_precision_loss)]
    let bars = opens
        .into_iter()
        .enumerate()
        .map(|(i, ts)| bar(ts, 4800.0 + i as f64))
        .collect();
    JoinedSeries::new("USA500IDXUSD", BarTimeframe::H1, bars).unwrap()
}

/// Sunday 2024-03-31's evening session: bars 120, 121 and 122 at mids 4920 to 4922.
fn sunday_session() -> [DateTime<Utc>; 3] {
    [
        utc(2024, 3, 31, 21),
        utc(2024, 3, 31, 22),
        utc(2024, 3, 31, 23),
    ]
}

/// The days and kinds of the curve's points.
fn points(m: &Metrics) -> Vec<(NaiveDate, PointKind)> {
    m.equity.iter().map(|p| (p.date, p.kind)).collect()
}

/// Monday 25 to Friday 29 as weekday points, then `last` as the weekend close.
fn week_then(last: NaiveDate) -> Vec<(NaiveDate, PointKind)> {
    (25..=29)
        .map(|d| (date(2024, 3, d), PointKind::Weekday))
        .chain([(last, PointKind::WeekendClose)])
        .collect()
}

/// QA's first case on #31: a window ending on Sunday holds a Sunday round trip. The
/// curve gains a weekend close at the window's close, so nothing is dropped.
#[test]
fn a_sunday_ending_window_keeps_a_sunday_round_trip() {
    let s = week_of_25_march(&sunday_session());
    let mut l = ledger();
    // Monday 10:00 to 15:00 (net 10 × (5 − 0.6) USD = 35.2 GBP), then Sunday 21:00 to
    // 23:00 (net 10 × (2 − 0.6) USD = 11.2 GBP). Neither crosses a rollover.
    for (entry, exit) in [(10, 15), (120, 122)] {
        let id = l
            .open(&order(Direction::Long, 10.0), at_open(&s, entry))
            .unwrap();
        l.close(id, at_open(&s, exit), ExitReason::Signal, &s)
            .unwrap();
    }
    let cfg = MetricsConfig::new(date(2024, 3, 25), date(2024, 3, 31), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    assert_eq!(points(&m), week_then(date(2024, 3, 31)));
    let (fri, sun) = (&m.equity[4], &m.equity[5]);
    assert_eq!(sun.at, utc(2024, 4, 1, 0));
    // Friday holds Monday's trade only; the Sunday trade is in the weekend close.
    assert!(close_to(fri.realised_net, 35.2));
    assert!(close_to(sun.realised_net, 35.2 + 11.2));
    assert_eq!(m.trades.count, 2);
    assert!(close_to(m.trades.net_pnl, 35.2 + 11.2));
    assert!(close_to(m.final_equity - CAPITAL, m.trades.net_pnl));
    // Its return runs from Friday's close and is a daily observation like the others.
    assert!(close_to(
        sun.simple_return.unwrap(),
        11.2 / (CAPITAL + 35.2)
    ));
    assert_eq!(m.returns.observations, 6);
    assert_eq!(m.daily_returns().unwrap().len(), 6);
    assert_identities(&m);
}

/// QA's second case on #31: a long opened on Sunday evening and still open at the end
/// of a Sunday-ending window is in the final equity.
#[test]
fn a_sunday_ending_window_values_a_position_opened_on_sunday() {
    let s = week_of_25_march(&sunday_session());
    let mut l = ledger();
    l.open(&order(Direction::Long, 10.0), at_open(&s, 120))
        .unwrap();
    let cfg = MetricsConfig::new(date(2024, 3, 25), date(2024, 3, 31), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    // Bought at 4920.3; liquidated at the 23:00 bar's close, selling at 4922 − 0.3.
    let end = &m.open_at_end;
    assert_eq!(end.count, 1);
    assert_eq!(end.positions[0].mark_bar_open, utc(2024, 3, 31, 23));
    assert!(close_to(end.unrealised_net, 0.8 * 10.0 * 1.4));
    assert!(close_to(end.exit_cost, 0.8 * 10.0 * 0.3));
    assert!(close_to(m.final_equity - CAPITAL, end.unrealised_net));

    assert_eq!(points(&m), week_then(date(2024, 3, 31)));
    let (fri, sun) = (&m.equity[4], &m.equity[5]);
    assert_eq!((fri.open_positions, fri.equity), (0, CAPITAL));
    assert_eq!(sun.open_positions, end.count);
    assert!(close_to(sun.unrealised_net, end.unrealised_net));
    assert!(close_to(sun.open_exit_cost, end.exit_cost));
    assert_identities(&m);
}

/// A window ending on Saturday closes on Saturday. A Friday long closed on a Saturday
/// bar is realised there; with nothing after Friday's close, the point repeats Friday's.
#[test]
fn a_saturday_ending_window_closes_on_saturday() {
    let s = week_of_25_march(&[utc(2024, 3, 30, 10)]);
    let mut l = ledger();
    // Friday 12:00 (mid 4908) to the Saturday 10:00 bar (mid 4920), across Friday's
    // triple-day rollover.
    let id = l
        .open(&order(Direction::Long, 1.0), at_open(&s, 108))
        .unwrap();
    let t = l
        .close(id, at_open(&s, 120), ExitReason::Signal, &s)
        .unwrap()
        .clone();
    assert_eq!(t.financing_days, 3);
    let cfg = MetricsConfig::new(date(2024, 3, 25), date(2024, 3, 30), CAPITAL);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();

    assert_eq!(points(&m), week_then(date(2024, 3, 30)));
    let (fri, sat) = (&m.equity[4], &m.equity[5]);
    assert_eq!(sat.at, utc(2024, 3, 31, 0));
    assert_eq!(fri.open_positions, 1);
    assert_eq!(sat.open_positions, 0);
    assert!(close_to(sat.equity, CAPITAL + t.net_pnl_account));
    assert!(close_to(m.final_equity - CAPITAL, m.trades.net_pnl));
    assert!(close_to(sat.costs.financing, 0.8 * t.financing));
    assert_identities(&m);

    // The same long left open, with no Saturday bar: the point repeats Friday's.
    let s = week_of_25_march(&[]);
    let mut l = ledger();
    l.open(&order(Direction::Long, 1.0), at_open(&s, 108))
        .unwrap();
    let m = Metrics::compute(&l, &s, &cfg).unwrap();
    let (fri, sat) = (&m.equity[4], &m.equity[5]);
    assert_eq!(sat.kind, PointKind::WeekendClose);
    assert_eq!(sat.equity, fri.equity);
    assert_eq!(sat.simple_return, Some(0.0));
    assert!(close_to(
        m.final_equity - CAPITAL,
        m.open_at_end.unrealised_net
    ));
    assert_identities(&m);
}

#[test]
fn ruin_is_reported_and_leaves_the_return_statistics_undefined() {
    let s = rising(utc(2024, 1, 16, 0), 48);
    let mut l = ledger();
    let id = l
        .open(&order(Direction::Short, 10.0), at_open(&s, 10))
        .unwrap();
    l.close(id, at_open(&s, 15), ExitReason::Stop, &s).unwrap();
    let cfg = MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 17), 30.0);
    let m = Metrics::compute(&l, &s, &cfg).unwrap();
    assert_eq!(m.ruined_on, Some(date(2024, 1, 16)));
    assert!(close_to(m.final_equity, 30.0 - 44.8));
    assert_eq!(m.returns, ReturnStats::undefined(2));
    assert_eq!(m.daily_returns(), None);
    assert_eq!(m.equity[1].simple_return, None);
    let dd = m.max_drawdown.by_amount.unwrap();
    assert_eq!(dd.fraction, -1.0);
    assert!(close_to(dd.amount, -44.8));
}

#[test]
fn invalid_configs_and_unmarkable_runs_are_refused() {
    let s = rising(utc(2024, 1, 16, 0), 24);
    let mut l = ledger();
    let (tue, sat, sun) = (date(2024, 1, 16), date(2024, 1, 20), date(2024, 1, 21));
    let run = |l: &Ledger, cfg: MetricsConfig| Metrics::compute(l, &s, &cfg).unwrap_err();
    assert_eq!(
        run(&l, MetricsConfig::new(tue, tue, 0.0)),
        MetricsError::InvalidCapital(0.0)
    );
    assert!(matches!(
        run(&l, MetricsConfig::new(tue, tue, f64::NAN)),
        MetricsError::InvalidCapital(_)
    ));
    assert!(matches!(
        run(
            &l,
            MetricsConfig {
                risk_free_rate: f64::INFINITY,
                ..MetricsConfig::new(tue, tue, CAPITAL)
            }
        ),
        MetricsError::InvalidRiskFreeRate(_)
    ));
    assert!(matches!(
        run(&l, MetricsConfig::new(sat, tue, CAPITAL)),
        MetricsError::EmptyWindow { .. }
    ));
    assert!(matches!(
        run(&l, MetricsConfig::new(sat, sun, CAPITAL)),
        MetricsError::NoTradingDay { .. }
    ));

    l.open(&order(Direction::Long, 1.0), at_open(&s, 3))
        .unwrap();
    // A fill before the window.
    assert!(matches!(
        run(
            &l,
            MetricsConfig::new(date(2024, 1, 17), date(2024, 1, 17), CAPITAL)
        ),
        MetricsError::OutsideWindow { .. }
    ));
    // A mark source that has never heard of the instrument.
    let other = JoinedSeries::new("EURUSD", BarTimeframe::H1, vec![]).unwrap();
    assert!(matches!(
        Metrics::compute(&l, &other, &MetricsConfig::new(tue, tue, CAPITAL)),
        Err(MetricsError::Ledger(LedgerError::NoMark { .. }))
    ));
}

#[test]
fn metrics_round_trip_through_json_with_their_conventions() {
    let s = rising(utc(2024, 1, 16, 12), 72);
    let mut l = ledger();
    let id = l
        .open(&order(Direction::Long, 2.0), at_open(&s, 0))
        .unwrap();
    l.close(id, at_open(&s, 30), ExitReason::Target, &s)
        .unwrap();
    l.open(&order(Direction::Short, 1.0), at_open(&s, 40))
        .unwrap();
    let cfg = MetricsConfig {
        risk_free_rate: 0.04,
        ..MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 18), CAPITAL)
    };
    let m = Metrics::compute(&l, &s, &cfg).unwrap();
    let json = serde_json::to_string(&m).unwrap();
    for needle in [
        r#""day":"utc_weekday_close""#,
        r#""mark":"liquidation""#,
        r#""std_ddof":1"#,
        r#""risk_free_rate":0.04"#,
        r#""account_currency":"GBP""#,
        r#""mode":"intrabar""#,
        r#""venue":"ig_spread_bet""#,
        r#""component":"carry""#,
        r#""effect":"omitted""#,
        r#""USA500IDXUSD":true"#,
    ] {
        assert!(json.contains(needle), "{needle} not in {json}");
    }
    let back: Metrics = serde_json::from_str(&json).unwrap();
    assert_eq!(
        serde_json::to_value(&back).unwrap(),
        serde_json::from_str::<serde_json::Value>(&json).unwrap()
    );
    assert_eq!(back.conventions, m.conventions);
    assert_eq!(back.equity.len(), 3);

    // Every point says what it closes; a record from before `kind` existed reads back
    // with weekday points.
    assert!(json.contains(r#""kind":"weekday""#), "{json}");
    let older: Metrics = serde_json::from_str(&json.replace(r#""kind":"weekday","#, "")).unwrap();
    assert_eq!(
        serde_json::to_value(&older).unwrap(),
        serde_json::to_value(&back).unwrap()
    );
}

#[test]
fn trade_statistics_count_streaks_in_closing_order() {
    // Win, loss, loss, win, loss, then a win: losing streak 2, winning streak 1.
    let s = rising(utc(2024, 1, 16, 0), 24);
    let mut l = ledger();
    for (i, direction) in [
        Direction::Long,
        Direction::Short,
        Direction::Short,
        Direction::Long,
        Direction::Short,
        Direction::Long,
    ]
    .into_iter()
    .enumerate()
    {
        let id = l.open(&order(direction, 1.0), at_open(&s, 2 * i)).unwrap();
        l.close(id, at_open(&s, 2 * i + 2), ExitReason::Signal, &s)
            .unwrap();
    }
    let m = Metrics::compute(
        &l,
        &s,
        &MetricsConfig::new(date(2024, 1, 16), date(2024, 1, 16), CAPITAL),
    )
    .unwrap();
    let t = &m.trades;
    // Each long: +2 points less 0.6 of costs = 1.4 USD; each short: -2.6 USD.
    assert_eq!((t.count, t.wins, t.losses), (6, 3, 3));
    assert_eq!((t.longest_winning_streak, t.longest_losing_streak), (1, 2));
    assert!(close_to(t.profit_factor.unwrap(), 1.4 / 2.6));
    assert!(close_to(
        t.expectancy.unwrap(),
        0.8 * (3.0 * 1.4 - 3.0 * 2.6) / 6.0
    ));
    assert_eq!(t.by_exit_reason.signal.count, 6);
}

proptest! {
    #![proptest_config(ProptestConfig::with_cases(64))]

    /// One point per weekday in the window, plus a weekend close when the window ends on
    /// a Saturday or Sunday, whatever the window and the trade.
    #[test]
    fn the_curve_has_one_point_per_weekday_and_one_for_a_weekend_close(
        start in 0u64..60,
        len in 0u64..30,
        entry in 0usize..200,
        hold in 1usize..400,
        long in any::<bool>(),
    ) {
        let first = date(2024, 1, 1) + chrono::Days::new(start);
        let last = first + chrono::Days::new(len);
        let hours = i64::try_from((len + 1) * 24).unwrap();
        let s = rising(equity::day_start(first), hours);
        let n = s.len();
        let mut l = ledger();
        let direction = if long { Direction::Long } else { Direction::Short };
        let entry = entry % n;
        let id = l.open(&order(direction, 1.0), at_open(&s, entry)).unwrap();
        if entry + hold < n {
            l.close(id, at_open(&s, entry + hold), ExitReason::Signal, &s).unwrap();
        }
        let weekdays = first
            .iter_days()
            .take_while(|d| *d <= last)
            .filter(|d| !matches!(d.weekday(), Weekday::Sat | Weekday::Sun))
            .count();
        let weekend_end = matches!(last.weekday(), Weekday::Sat | Weekday::Sun);
        let expected = weekdays + usize::from(weekdays > 0 && weekend_end);
        match Metrics::compute(&l, &s, &MetricsConfig::new(first, last, CAPITAL)) {
            Ok(m) => {
                prop_assert_eq!(m.equity.len(), expected);
                prop_assert_eq!(m.returns.observations, expected);
                prop_assert_eq!(m.trades.count + m.open_at_end.count, 1);
                let end = m.equity.last().unwrap();
                prop_assert_eq!(end.at, equity::day_start(last + chrono::Days::new(1)));
                let kind = if weekend_end { PointKind::WeekendClose } else { PointKind::Weekday };
                prop_assert_eq!((end.date, end.kind), (last, kind));
                assert_identities(&m);
            }
            Err(e) => {
                prop_assert_eq!(weekdays, 0);
                prop_assert!(matches!(e, MetricsError::NoTradingDay { .. }), "{e}");
            }
        }
    }

    /// For every window `compute` accepts, weekend endings included, the final equity
    /// reconciles with the trade statistics and the positions open at the end:
    /// `final − capital = closed net + open net`, both net of financing, and equally
    /// `gross − costs − open exit cost` with every financing charge booked to the
    /// window's close among the costs.
    #[test]
    fn the_final_equity_reconciles_with_the_trades_and_open_positions(
        start in 0u64..60,
        len in 0u64..30,
        entries in (0usize..800, 0usize..800),
        hold in 1usize..400,
        long in any::<bool>(),
    ) {
        let first = date(2024, 1, 1) + chrono::Days::new(start);
        let last = first + chrono::Days::new(len);
        let close = equity::day_start(last + chrono::Days::new(1));
        let hours = i64::try_from((len + 1) * 24).unwrap();
        let s = rising(equity::day_start(first), hours);
        let n = s.len();
        let mut l = ledger();
        // A round trip, closed when it fits in the window, and a long left open.
        let direction = if long { Direction::Long } else { Direction::Short };
        let (a, b) = (entries.0 % n, entries.1 % n);
        let id = l.open(&order(direction, 1.0), at_open(&s, a)).unwrap();
        if a + hold < n {
            l.close(id, at_open(&s, a + hold), ExitReason::Signal, &s).unwrap();
        }
        l.open(&order(Direction::Long, 2.0), at_open(&s, b)).unwrap();
        let m = match Metrics::compute(&l, &s, &MetricsConfig::new(first, last, CAPITAL)) {
            Ok(m) => m,
            Err(e) => {
                prop_assert!(matches!(e, MetricsError::NoTradingDay { .. }), "{e}");
                return Ok(());
            }
        };
        let end = m.equity.last().unwrap();
        let open = &m.open_at_end;
        let pnl = m.final_equity - CAPITAL;
        prop_assert_eq!(end.at, close);
        prop_assert!(close_to(pnl, m.trades.net_pnl + open.unrealised_net));
        prop_assert!(close_to(end.realised_net, m.trades.net_pnl));
        prop_assert!(close_to(end.unrealised_net, open.unrealised_net));
        prop_assert_eq!(end.open_positions, open.count);
        prop_assert!(close_to(end.open_exit_cost, open.exit_cost));
        prop_assert!(close_to(
            pnl,
            end.gross_pnl - end.costs.total() - end.open_exit_cost
        ));
        let mut accrued = l.clone();
        accrued.accrue_financing(close, &s).unwrap();
        let charged: f64 = accrued.financing_charges().iter().map(|c| 0.8 * c.charge).sum();
        prop_assert!(close_to(end.costs.financing, charged));
        assert_identities(&m);
    }
}
