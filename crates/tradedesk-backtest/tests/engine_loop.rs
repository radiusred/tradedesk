//! The single-run engine on synthetic daily gold bars, driving a real `Ledger` and the
//! test strategy (`toy::ThresholdCross`) through `engine::run`.
#![allow(clippy::float_cmp)]

mod synthetic;
mod toy;

use chrono::{Datelike, Duration, NaiveDate};
use synthetic::{Day, MemReader, breakout_path, first_day};
use toy::{ThresholdConfig, ThresholdCross};
use tradedesk_backtest::engine::{self, EndOfRun, EngineConfig, RunOutput, SizingPolicy};
use tradedesk_backtest::strategy::{Entry, SignalExitReason, Strategy};
use tradedesk_backtest::{
    AccountFx, BarTimeframe, BothHit, CostConfig, Direction, EndOfRunRule, ExitEvaluation,
    ExitReason, FinalBarEntry, LoadRequest, MarketData, MetricsConfig, OneSidedPolicy, PointKind,
    load,
};

const SYMBOL: &str = "XAUUSD";
const INTRABAR: ExitEvaluation = ExitEvaluation::Intrabar {
    both_hit: BothHit::StopFirst,
};

fn data(days: &[Day]) -> (MarketData, NaiveDate) {
    let last = first_day() + Duration::days(i64::try_from(days.len()).unwrap() - 1);
    let reader = MemReader::default().with_days(SYMBOL, first_day(), days);
    let request = LoadRequest::utc_days(SYMBOL, first_day(), last, [BarTimeframe::D1]);
    (
        load(&reader, &[request], OneSidedPolicy::Reject).unwrap(),
        last,
    )
}

/// The test strategy on the breakout path: enters on the cross of $2030 (the day-60
/// breakout), its stop $50 under the entry close and its target `target_distance` (raw
/// cents) over it, and exits on a close under $2030.
fn threshold(target_distance: f64) -> ThresholdCross {
    ThresholdCross::new(ThresholdConfig {
        target_distance,
        ..ThresholdConfig::default()
    })
    .unwrap()
}

/// Three times the stop distance: the default target.
const TARGET: f64 = 15_000.0;

fn fx() -> AccountFx {
    AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap()
}

fn config(first: NaiveDate, last: NaiveDate, eval: ExitEvaluation, end: EndOfRun) -> EngineConfig {
    EngineConfig {
        instrument: SYMBOL.to_owned(),
        venue: "ig_spread_bet".to_owned(),
        exit_evaluation: eval,
        account_fx: fx(),
        sizing: SizingPolicy::StakePerPoint { stake: 1.0 },
        end_of_run: end,
        metrics: MetricsConfig::new(first, last, 25_000.0),
    }
}

/// Run the test strategy over the breakout path followed by `after`.
fn run_with(
    after: &[Day],
    eval: ExitEvaluation,
    end: EndOfRun,
    target_distance: f64,
) -> (RunOutput, ThresholdCross) {
    let (data, last) = data(&breakout_path(after));
    let mut strategy = threshold(target_distance);
    let out = engine::run(
        &mut strategy,
        &data,
        &CostConfig::builtin(),
        &config(first_day(), last, eval, end),
    )
    .unwrap();
    (out, strategy)
}

fn run(after: &[Day], eval: ExitEvaluation, end: EndOfRun) -> (RunOutput, ThresholdCross) {
    run_with(after, eval, end, TARGET)
}

#[test]
fn an_entry_fills_at_the_close_and_is_bridged_back_in_raw_units() {
    let (out, strategy) = run(
        &[(2060.0, 2064.0, 2052.0, 2058.0)],
        INTRABAR,
        EndOfRun::LeaveOpen,
    );
    assert_eq!(out.stats.entries, 1);
    let fill = &out.ledger.fills()[0];
    // Entered at the breakout bar's close: 2024-03-01 (day 60) closes at 03-02 00:00Z.
    assert_eq!(
        fill.ts.date_naive(),
        NaiveDate::from_ymd_opt(2024, 3, 2).unwrap()
    );
    assert_eq!(fill.raw_price, 206_000.0);
    // £1 a point on gold (pip $0.01) at USD→GBP 0.8 is 125 units.
    assert_eq!(fill.size, 125.0);
    let position = &out.ledger.open_positions()[0];
    let raw_fill = fill.fill_price / 0.01;
    assert!(raw_fill > 206_000.0, "a buy fills above the mark");
    // The levels moved with the fill, keeping their distances from it: the target is
    // three times as far above it as the stop is below.
    let stop = position.stop.unwrap();
    let target = position.target.unwrap();
    assert!(stop < raw_fill && target > raw_fill);
    assert!(((raw_fill - stop) * 3.0 - (target - raw_fill)).abs() < 1e-6);
    assert_eq!(strategy.position(), Some(Direction::Long));
    assert_eq!(out.metrics.open_at_end.count, 1);
}

#[test]
fn an_intrabar_stop_closes_once_and_flattens_the_strategy() {
    let (out, strategy) = run(
        &[
            (2060.0, 2064.0, 1940.0, 2050.0),
            (2050.0, 2056.0, 2044.0, 2052.0),
        ],
        INTRABAR,
        EndOfRun::Close,
    );
    let trades = out.ledger.closed_trades();
    assert_eq!(trades.len(), 1);
    assert_eq!(trades[0].exit_reason, ExitReason::Stop);
    assert_eq!(trades[0].signal_reason, None);
    assert_eq!(out.stats.ledger_exits, 1);
    assert_eq!(strategy.position(), None);
    assert!(trades[0].net_pnl_account < 0.0);
}

#[test]
fn a_gap_through_the_stop_fills_at_the_open_and_is_stamped_with_it() {
    let (out, _) = run(
        &[(1940.0, 1945.0, 1930.0, 1940.0)],
        INTRABAR,
        EndOfRun::Close,
    );
    let trade = &out.ledger.closed_trades()[0];
    assert_eq!(trade.exit_reason, ExitReason::Stop);
    let exit = &out.ledger.fills()[1];
    assert_eq!(
        exit.raw_price, 194_000.0,
        "filled at the open, worse than the stop"
    );
    // The D1 bar opens at the instant the entry (the previous close) was stamped.
    assert_eq!(exit.ts, trade.entry_ts);
    assert_eq!(trade.bars_held, 1);
}

#[test]
fn an_intrabar_target_fills_at_its_level() {
    let (out, _) = run_with(
        &[(2060.0, 2400.0, 2055.0, 2100.0)],
        INTRABAR,
        EndOfRun::Close,
        5_000.0,
    );
    let trade = &out.ledger.closed_trades()[0];
    assert_eq!(trade.exit_reason, ExitReason::Target);
    let exit_fill = &out.ledger.fills()[1];
    let target = trade.exit_mark / 0.01;
    assert_eq!(exit_fill.raw_price, target);
    assert!(target < 240_000.0 && target > 206_000.0);
    assert!(trade.net_pnl_account > 0.0);
}

#[test]
fn a_strategy_exit_is_booked_as_signal_with_its_label() {
    // Closes under $2030 but over the stop: the strategy exits, the ledger does not.
    let (out, strategy) = run(
        &[(2060.0, 2060.0, 2016.0, 2020.0)],
        ExitEvaluation::CloseOnly,
        EndOfRun::Close,
    );
    let trade = &out.ledger.closed_trades()[0];
    assert_eq!(trade.exit_reason, ExitReason::Signal);
    assert_eq!(trade.signal_reason, Some(SignalExitReason::StopLoss));
    assert_eq!(out.stats.strategy_exits, 1);
    assert_eq!(out.stats.end_of_run_closes, 0);
    assert_eq!(strategy.position(), None);
}

/// Five quiet days after the entry (Saturday 2 March to Wednesday 6 March).
const QUIET: [Day; 5] = [(2062.0, 2066.0, 2058.0, 2063.0); 5];

#[test]
fn financing_is_charged_for_each_night_held() {
    let (out, _) = run(&QUIET, INTRABAR, EndOfRun::LeaveOpen);
    // Held from Sat 2 March 00:00Z to the last close (Thu 7 March 00:00Z): the IG
    // rollovers on Mon, Tue and Wed at 22:00 London, one day each (metals triple on
    // Friday). The engine accrued them bar by bar on its own ledger.
    let position = &out.ledger.open_positions()[0];
    assert_eq!(position.financing_days, 3);
    assert_eq!(out.ledger.financing_charges().len(), 3);
    assert!(position.financing > 0.0, "a long pays the admin fee");
    for charge in out.ledger.financing_charges() {
        assert_eq!(
            charge.mark_price, 2063.0,
            "marked at the day's last 1m close"
        );
    }
}

#[test]
fn end_of_run_closes_or_leaves_the_position_as_configured() {
    let (closed, s1) = run(&QUIET, INTRABAR, EndOfRun::Close);
    let trade = &closed.ledger.closed_trades()[0];
    assert_eq!(trade.exit_reason, ExitReason::EndOfRun);
    assert_eq!(trade.financing_days, 3);
    assert_eq!(closed.stats.end_of_run_closes, 1);
    assert_eq!(closed.metrics.open_at_end.count, 0);
    assert_eq!(s1.position(), None);

    let (left, s2) = run(&QUIET, INTRABAR, EndOfRun::LeaveOpen);
    assert!(left.ledger.closed_trades().is_empty());
    assert_eq!(left.metrics.open_at_end.count, 1);
    assert_eq!(s2.position(), Some(Direction::Long));
    // Both mark the same position at liquidation on the last day.
    let (a, b) = (closed.metrics.final_equity, left.metrics.final_equity);
    assert!((a - b).abs() < 1e-6, "{a} vs {b}");
}

#[test]
fn warm_up_bars_reach_the_strategy_but_never_trade() {
    let (data, last) = data(&breakout_path(&QUIET));
    let mut strategy = threshold(TARGET);
    let trade_from = NaiveDate::from_ymd_opt(2024, 3, 2).unwrap();
    let out = engine::run(
        &mut strategy,
        &data,
        &CostConfig::builtin(),
        &config(trade_from, last, INTRABAR, EndOfRun::Close),
    )
    .unwrap();
    assert_eq!(out.stats.warmup_bars, 61);
    assert_eq!(out.stats.warmup_entries_rejected, 1);
    assert_eq!(out.stats.entries, 0);
    assert!(out.ledger.fills().is_empty());
    assert_eq!(out.metrics.first_day, trade_from);
}

#[test]
fn a_run_is_deterministic() {
    let after = [
        (2060.0, 2064.0, 1940.0, 2050.0),
        (2050.0, 2056.0, 2044.0, 2052.0),
    ];
    let (a, _) = run(&after, INTRABAR, EndOfRun::Close);
    let (b, _) = run(&after, INTRABAR, EndOfRun::Close);
    assert_eq!(a.ledger.fills(), b.ledger.fills());
    assert_eq!(a.ledger.closed_trades(), b.ledger.closed_trades());
    assert_eq!(a.metrics, b.metrics);
    assert_eq!(a.stats, b.stats);
}

#[test]
fn a_divergent_or_missing_input_fails_the_run_instead_of_falling_back() {
    let (data, last) = data(&breakout_path(&QUIET));
    let mut strategy = threshold(TARGET);
    let mut cfg = config(first_day(), last, INTRABAR, EndOfRun::Close);
    cfg.instrument = "EURUSD".to_owned();
    assert!(matches!(
        engine::run(&mut strategy, &data, &CostConfig::builtin(), &cfg),
        Err(engine::EngineError::MissingSeries { .. })
    ));
    cfg.instrument = SYMBOL.to_owned();
    cfg.sizing = SizingPolicy::StakePerPoint { stake: 0.0 };
    assert!(matches!(
        engine::run(&mut threshold(TARGET), &data, &CostConfig::builtin(), &cfg),
        Err(engine::EngineError::Sizing(_))
    ));
}

#[test]
fn sizing_policies_give_the_documented_units() {
    let costs = CostConfig::builtin();
    let ledger = tradedesk_backtest::Ledger::new(&costs, "ig_spread_bet", INTRABAR, fx()).unwrap();
    let model = ledger.fill_model(SYMBOL).unwrap();
    let entry = Entry {
        direction: Direction::Long,
        reference_price: 206_000.0,
        stop: None,
        target: None,
        atr: 2_500.0, // $25 in cents
    };
    let stake = SizingPolicy::StakePerPoint { stake: 2.0 };
    assert_eq!(stake.units(&entry, model, &fx()).unwrap(), 250.0);
    // £100 over a 2-ATR move: 100 / (2500 cents × 2 × (0.01 × 0.8)) = 2.5 units.
    let atr = SizingPolicy::AtrRisk {
        risk: 100.0,
        atr_multiple: 2.0,
        min_units: 0.1,
        max_units: 1000.0,
    };
    assert!((atr.units(&entry, model, &fx()).unwrap() - 2.5).abs() < 1e-12);
    let capped = SizingPolicy::AtrRisk {
        risk: 100.0,
        atr_multiple: 2.0,
        min_units: 0.1,
        max_units: 1.0,
    };
    assert_eq!(capped.units(&entry, model, &fx()).unwrap(), 1.0);
    let inverted = SizingPolicy::AtrRisk {
        risk: 100.0,
        atr_multiple: 2.0,
        min_units: 5.0,
        max_units: 1.0,
    };
    assert!(inverted.units(&entry, model, &fx()).is_err());
}

/// Day `i` of the synthetic data (day 0 is Monday 2024-01-01).
fn day(i: usize) -> NaiveDate {
    first_day() + Duration::days(i64::try_from(i).unwrap())
}

/// Run `strategy` over `days` with the window opening on day `window_from`: everything
/// before it is warm-up.
fn run_from<S: Strategy>(strategy: &mut S, days: &[Day], window_from: usize) -> RunOutput {
    let (data, last) = data(days);
    engine::run(
        strategy,
        &data,
        &CostConfig::builtin(),
        &config(day(window_from), last, INTRABAR, EndOfRun::Close),
    )
    .unwrap()
}

#[test]
fn a_rejected_warm_up_entry_honours_the_strategy_cooldown() {
    // After the 60 quiet days, eighteen days inside warm-up that zig-zag across $2030
    // (closes $2040, $2020, ...): the entry condition holds on the nine up-crosses,
    // every other bar.
    let mut days = breakout_path(&[]);
    days.truncate(60);
    let cents = |c: f64| c * 100.0;
    let mut prev = 2006.0;
    for k in 0..18_u8 {
        let c = if k % 2 == 0 { 2040.0 } else { 2020.0 };
        days.push((
            cents(prev),
            cents(prev.max(c) + 5.0),
            cents(prev.min(c) - 5.0),
            cents(c),
        ));
        prev = c;
    }
    let window_from = days.len();
    days.extend([(cents(2020.0), cents(2025.0), cents(2015.0), cents(2020.0)); 5]);
    let rejected = |cooldown_bars: u32| {
        let mut strategy = ThresholdCross::new(ThresholdConfig {
            cooldown_bars,
            ..ThresholdConfig::default()
        })
        .unwrap();
        let out = run_from(&mut strategy, &days, window_from);
        assert_eq!(out.stats.warmup_bars, window_from);
        assert_eq!(out.stats.entries, 0, "nothing crosses inside the window");
        out.stats.warmup_entries_rejected
    };
    // A 1-bar cooldown allows the next bar after an exit, so a rejection does too: every
    // up-cross is signalled and rejected.
    assert_eq!(rejected(1), 9);
    // A 3-bar cooldown: rejected on the up-crosses at days 60, 64, 68, 72 and 76, not
    // on the ones at 62, 66, 70 and 74, which fall inside it.
    assert_eq!(rejected(3), 5);
    // 9 bars: days 60 and 70 only.
    assert_eq!(rejected(9), 2);
    // A cooldown longer than the zig-zag: the first up-cross only.
    assert_eq!(rejected(20), 1);
}

/// Quiet days (the `breakout_path` alternation) up to `on_day`, which breaks out to a
/// $2060 close: the test strategy's entry lands on the last bar of the data.
fn breakout_on(on_day: usize) -> Vec<Day> {
    let mut days = breakout_path(&[]);
    days.truncate(60);
    for i in 60..on_day {
        let (prev, c) = if i % 2 == 1 {
            (2000.0, 2006.0)
        } else {
            (2006.0, 2000.0)
        };
        days.push((prev * 100.0, 201_600.0, 199_000.0, c * 100.0));
    }
    let prev = days.last().unwrap().3;
    days.push((prev, 207_000.0, prev - 1000.0, 206_000.0));
    days
}

/// Run the test strategy over `days` (window = all of them) under `end`, and check the rule for
/// an entry on the final bar: filled at the final close, then handled by `end`, with
/// the curve's last point reconciling either way.
fn final_bar_entry(days: &[Day], end: EndOfRun) -> RunOutput {
    let (data, last) = data(days);
    let mut strategy = threshold(TARGET);
    let out = engine::run(
        &mut strategy,
        &data,
        &CostConfig::builtin(),
        &config(first_day(), last, INTRABAR, end),
    )
    .unwrap();
    let window_close = (last + Duration::days(1))
        .and_hms_opt(0, 0, 0)
        .unwrap()
        .and_utc();
    assert_eq!(out.stats.entries, 1);
    assert_eq!(out.stats.final_bar_entries, 1);
    assert_eq!(out.metrics.run.end_of_run, Some(EndOfRunRule::new(end)));
    assert_eq!(
        out.metrics.run.end_of_run.unwrap().final_bar_entry,
        FinalBarEntry::FilledThenEndOfRun
    );
    // Filled at the final bar's close, which is the window's close.
    let entry = &out.ledger.fills()[0];
    assert_eq!(entry.ts, window_close);
    assert!(entry.spread_cost > 0.0, "the entry cost is charged");
    let last_point = out.metrics.equity.last().unwrap();
    assert_eq!(last_point.at, window_close);
    let m = &out.metrics;
    let booked = m.trades.net_pnl + m.open_at_end.unrealised_net;
    assert!(
        (m.final_equity - m.starting_capital - booked).abs() < 1e-6,
        "{} vs {booked}",
        m.final_equity - m.starting_capital
    );
    assert!(booked < 0.0, "nothing gained, both sides' costs paid");
    out
}

fn check_close_and_leave_open(days: &[Day]) -> PointKind {
    let closed = final_bar_entry(days, EndOfRun::Close);
    let trade = &closed.ledger.closed_trades()[0];
    assert_eq!(trade.exit_reason, ExitReason::EndOfRun);
    assert_eq!(trade.bars_held, 0);
    assert_eq!(trade.entry_ts, trade.exit_ts);
    assert_eq!(trade.gross_pnl_account, 0.0);
    let costs = trade.gross_pnl_account - trade.net_pnl_account;
    assert!(costs > 0.0, "the full exit cost on top of the entry cost");
    assert_eq!(closed.stats.end_of_run_closes, 1);
    assert_eq!(closed.metrics.open_at_end.count, 0);
    assert_eq!(closed.metrics.trades.count, 1);

    let left = final_bar_entry(days, EndOfRun::LeaveOpen);
    assert!(left.ledger.closed_trades().is_empty());
    assert_eq!(left.stats.end_of_run_closes, 0);
    assert_eq!(left.metrics.open_at_end.count, 1);
    assert_eq!(left.metrics.trades.count, 0);
    // The same liquidation either way.
    assert!(
        (closed.metrics.final_equity - left.metrics.final_equity).abs() < 1e-6,
        "{} vs {}",
        closed.metrics.final_equity,
        left.metrics.final_equity
    );
    closed.metrics.equity.last().unwrap().kind
}

#[test]
fn an_entry_on_the_final_bar_follows_the_end_of_run_rule() {
    // Day 60 is Friday 2024-03-01: a weekday-ending window.
    assert_eq!(
        check_close_and_leave_open(&breakout_on(60)),
        PointKind::Weekday
    );
}

#[test]
fn an_entry_on_the_final_bar_of_a_weekend_ending_window_reconciles() {
    // #54's case: the window ends on Sunday 2024-03-03 and its final bar is Sunday's,
    // so the fill is at the window's close, inside the weekend-close point.
    assert_eq!(
        day(62).weekday(),
        chrono::Weekday::Sun,
        "the window ends on a Sunday"
    );
    assert_eq!(
        check_close_and_leave_open(&breakout_on(62)),
        PointKind::WeekendClose
    );
    // And on Saturday 2024-03-02.
    assert_eq!(day(61).weekday(), chrono::Weekday::Sat);
    assert_eq!(
        check_close_and_leave_open(&breakout_on(61)),
        PointKind::WeekendClose
    );
}

#[test]
fn an_entry_before_the_final_bar_is_not_counted_as_one() {
    let (out, _) = run(&QUIET, INTRABAR, EndOfRun::LeaveOpen);
    assert_eq!(out.stats.entries, 1);
    assert_eq!(out.stats.final_bar_entries, 0);
}

#[test]
fn the_engine_refuses_a_strategy_in_the_wrong_price_scale() {
    // XAUUSD is in cents (raw_scale 0.01). A strategy that declares it assumes raw_scale
    // 1 is refused before it runs; one that declares the instrument's scale runs.
    let (data, last) = data(&breakout_path(&QUIET));
    let cfg = config(first_day(), last, INTRABAR, EndOfRun::Close);
    let with_scale = |price_scale| {
        ThresholdCross::new(ThresholdConfig {
            price_scale,
            ..ThresholdConfig::default()
        })
        .unwrap()
    };
    let err = engine::run(
        &mut with_scale(Some(1.0)),
        &data,
        &CostConfig::builtin(),
        &cfg,
    )
    .unwrap_err();
    assert!(
        matches!(err, engine::EngineError::PriceScale { assumed, raw_scale, .. }
            if assumed == 1.0 && raw_scale == 0.01),
        "{err}"
    );
    assert!(err.to_string().contains("raw_scale 0.01"), "{err}");
    assert!(
        engine::run(
            &mut with_scale(Some(0.01)),
            &data,
            &CostConfig::builtin(),
            &cfg
        )
        .is_ok()
    );
    // A strategy that declares no scale is not checked.
    assert!(engine::run(&mut with_scale(None), &data, &CostConfig::builtin(), &cfg).is_ok());
}
