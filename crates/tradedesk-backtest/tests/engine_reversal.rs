//! The one-fill reversal (`SignalKind::Reverse`) through `engine::run`, on synthetic daily
//! gold bars and a scripted test strategy. Every price and level here is a labelled test
//! value, and the venue is the builtin example CFD venue (a per-fill commission, so each
//! leg's cost is visible).
#![allow(clippy::float_cmp)]

mod synthetic;

use std::collections::BTreeMap;

use chrono::{Duration, NaiveDate};
use synthetic::{Day, MemReader, first_day};
use tradedesk_backtest::engine::{self, EndOfRun, EngineConfig, EngineError, SizingPolicy};
use tradedesk_backtest::strategy::{
    BridgeError, Entry, PositionFeedback, Signal, SignalKind, Strategy, StrategyError, checked_side,
};
use tradedesk_backtest::{
    AccountFx, BarTimeframe, BothHit, CostConfig, Direction, ExitEvaluation, ExitReason, JoinedBar,
    LoadRequest, MetricsConfig, OneSidedPolicy, SignalExitReason, load,
};
use tradedesk_data::Side;

const SYMBOL: &str = "XAUUSD";
const VENUE: &str = "example_ny_cfd";
const INTRABAR: ExitEvaluation = ExitEvaluation::Intrabar {
    both_hit: BothHit::StopFirst,
};
/// Test value: the scripted entries' stop distance, raw units (cents).
const STOP: f64 = 5_000.0;

/// What the scripted strategy does on a bar.
#[derive(Debug, Clone, Copy)]
enum Step {
    Enter(Direction),
    Exit,
    Reverse(Direction),
}

/// A test strategy that emits the signals it is given, on the primary bars it is given
/// (counted from 0), and keeps the position those signals imply. Entries carry a stop
/// `STOP` away from the bid close and no target. It records the feedback it receives.
struct Scripted {
    script: BTreeMap<usize, Step>,
    bar: usize,
    position: Option<Direction>,
    feedback: Vec<PositionFeedback>,
}

impl Scripted {
    fn new(script: &[(usize, Step)]) -> Self {
        Self {
            script: script.iter().copied().collect(),
            bar: 0,
            position: None,
            feedback: Vec::new(),
        }
    }
}

impl Strategy for Scripted {
    fn name(&self) -> &'static str {
        "scripted"
    }

    fn primary_timeframe(&self) -> BarTimeframe {
        BarTimeframe::D1
    }

    fn on_bar(&mut self, bar: &JoinedBar) -> Result<Option<Signal>, StrategyError> {
        let close = checked_side(bar, Side::Bid)?.close;
        let i = self.bar;
        self.bar += 1;
        let entry = |direction: Direction| Entry {
            direction,
            reference_price: close,
            stop: Some(close - direction.sign() * STOP),
            target: None,
            atr: STOP,
        };
        let kind = match self.script.get(&i) {
            None => return Ok(None),
            Some(&Step::Enter(d)) => {
                self.position = Some(d);
                SignalKind::Enter(entry(d))
            }
            Some(&Step::Exit) => {
                self.position = None;
                SignalKind::Exit {
                    reason: SignalExitReason::StopLoss,
                }
            }
            Some(&Step::Reverse(d)) => {
                self.position = Some(d);
                SignalKind::Reverse {
                    reason: SignalExitReason::MidbandCross,
                    entry: entry(d),
                }
            }
        };
        Ok(Some(Signal {
            ts_open_utc: bar.ts_open_utc,
            kind,
        }))
    }

    fn on_feedback(&mut self, feedback: PositionFeedback) -> Result<(), StrategyError> {
        if self.position.is_none() {
            return Err(StrategyError::Feedback {
                reason: "no signalled position",
            });
        }
        if matches!(
            feedback,
            PositionFeedback::EntryRejected | PositionFeedback::ClosedExternally
        ) {
            self.position = None;
        }
        self.feedback.push(feedback);
        Ok(())
    }

    fn position(&self) -> Option<Direction> {
        self.position
    }
}

/// `n` quiet days alternating $2000 / $2006 closes, each $10 either side, in cents.
fn quiet(n: usize) -> Vec<Day> {
    let mut prev: f64 = 200_000.0;
    (0..n)
        .map(|i| {
            let c = if i % 2 == 1 { 200_600.0 } else { 200_000.0 };
            let day = (prev, prev.max(c) + 1_000.0, prev.min(c) - 1_000.0, c);
            prev = c;
            day
        })
        .collect()
}

fn day(i: usize) -> NaiveDate {
    first_day() + Duration::days(i64::try_from(i).unwrap())
}

/// Run `strategy` over `days`, the window opening on day `window_from` (earlier days are
/// warm-up).
fn run(
    strategy: &mut Scripted,
    days: &[Day],
    window_from: usize,
    end: EndOfRun,
) -> Result<engine::RunOutput, EngineError> {
    let last = day(days.len() - 1);
    let reader = MemReader::default().with_days(SYMBOL, first_day(), days);
    let request = LoadRequest::utc_days(SYMBOL, first_day(), last, [BarTimeframe::D1]);
    let data = load(&reader, &[request], OneSidedPolicy::Reject).unwrap();
    let config = EngineConfig {
        instrument: SYMBOL.to_owned(),
        venue: VENUE.to_owned(),
        exit_evaluation: INTRABAR,
        account_fx: AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap(),
        sizing: SizingPolicy::StakePerPoint { stake: 1.0 },
        end_of_run: end,
        metrics: MetricsConfig::new(day(window_from), last, 25_000.0),
    };
    engine::run(strategy, &data, &CostConfig::builtin(), &config)
}

#[test]
fn long_to_short_closes_and_opens_at_one_fill_with_both_legs_costed() {
    let mut strategy = Scripted::new(&[
        (2, Step::Enter(Direction::Long)),
        (5, Step::Reverse(Direction::Short)),
    ]);
    let out = run(&mut strategy, &quiet(10), 0, EndOfRun::LeaveOpen).unwrap();

    assert_eq!(out.stats.reversals, 1);
    assert_eq!(out.stats.entries, 2);
    assert_eq!(out.stats.strategy_exits, 1);
    assert_eq!(out.stats.ledger_exits, 0);

    // Two trades on the ledger: the long, closed, and the short, open.
    let closed = out.ledger.closed_trades();
    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].direction, Direction::Long);
    assert_eq!(closed[0].exit_reason, ExitReason::Signal);
    assert_eq!(
        closed[0].signal_reason,
        Some(SignalExitReason::MidbandCross)
    );
    assert_eq!(closed[0].bars_held, 3);
    let open = out.ledger.open_positions();
    assert_eq!(open.len(), 1);
    assert_eq!(open[0].direction, Direction::Short);

    // One fill: the exit and the entry share the bar-5 close, its timestamp and its bar,
    // so there is no flat bar between them.
    let fills = out.ledger.fills();
    assert_eq!(fills.len(), 3);
    let (exit, entry) = (&fills[1], &fills[2]);
    assert_eq!(exit.position, closed[0].id);
    assert_eq!(entry.position, open[0].id);
    assert_eq!(exit.exit_reason, Some(ExitReason::Signal));
    assert_eq!(entry.exit_reason, None);
    assert_eq!(exit.ts, entry.ts);
    assert_eq!(exit.ts, closed[0].exit_ts);
    assert_eq!(entry.ts, open[0].entry_ts);
    assert_eq!(open[0].entry_bar_index, 5);
    assert_eq!(exit.raw_price, entry.raw_price);
    assert_eq!(exit.ts.date_naive(), day(6), "bar 5 closes at day 6 00:00Z");

    // Each leg is priced and costed on its own: both are sells at the close, so each
    // pays the same half spread and the same per-fill commission as the opening buy.
    for fill in fills {
        assert!(fill.spread_cost > 0.0);
        assert!(fill.commission > 0.0);
        assert_eq!(fill.size, fills[0].size);
        assert_eq!(fill.spread_cost, fills[0].spread_cost);
        assert_eq!(fill.commission, fills[0].commission);
    }
    assert_eq!(exit.fill_price, entry.fill_price);
    assert!(
        entry.fill_price < entry.mark_price,
        "a sell fills under the mark"
    );
    assert_eq!(closed[0].commission, fills[0].commission + exit.commission);
    assert_eq!(open[0].entry_commission, entry.commission);

    // The strategy holds the short, anchored at its own fill.
    assert_eq!(strategy.position(), Some(Direction::Short));
    assert_eq!(strategy.feedback.len(), 2);
    assert_eq!(
        strategy.feedback[1],
        PositionFeedback::EntryFilled {
            price: entry.fill_price / 0.01
        }
    );
    // The shifted stop sits STOP above the short's raw fill.
    let raw_fill = entry.fill_price / 0.01;
    assert!((open[0].stop.unwrap() - (raw_fill + STOP)).abs() < 1e-6);
}

#[test]
fn short_to_long_and_back_leaves_no_flat_bar() {
    let mut strategy = Scripted::new(&[
        (1, Step::Enter(Direction::Short)),
        (4, Step::Reverse(Direction::Long)),
        (7, Step::Reverse(Direction::Short)),
    ]);
    let out = run(&mut strategy, &quiet(10), 0, EndOfRun::Close).unwrap();

    assert_eq!(out.stats.reversals, 2);
    assert_eq!(out.stats.entries, 3);
    assert_eq!(out.stats.strategy_exits, 2);
    assert_eq!(out.stats.end_of_run_closes, 1);
    let trades = out.ledger.closed_trades();
    let directions: Vec<_> = trades.iter().map(|t| t.direction).collect();
    assert_eq!(
        directions,
        [Direction::Short, Direction::Long, Direction::Short]
    );
    // Each position opens at the fill that closes the one before it.
    for pair in trades.windows(2) {
        assert_eq!(pair[0].exit_ts, pair[1].entry_ts);
    }
    assert_eq!(trades[2].exit_reason, ExitReason::EndOfRun);
    // Short to long: the closing buy and the opening buy fill at one price.
    let fills = out.ledger.fills();
    assert_eq!(fills.len(), 6);
    assert_eq!(fills[1].fill_price, fills[2].fill_price);
    assert!(
        fills[2].fill_price > fills[2].mark_price,
        "a buy fills over the mark"
    );
}

#[test]
fn a_reversal_on_a_bar_whose_stop_triggers_closes_at_the_stop_then_enters() {
    // Bar 5 trades $80 under its open: the long's $50 stop triggers intrabar, and the
    // strategy reverses at the same bar's close.
    let mut days = quiet(10);
    days[5].2 -= 8_000.0;
    let mut strategy = Scripted::new(&[
        (2, Step::Enter(Direction::Long)),
        (5, Step::Reverse(Direction::Short)),
    ]);
    let out = run(&mut strategy, &days, 0, EndOfRun::LeaveOpen).unwrap();

    assert_eq!(out.stats.reversals, 1);
    assert_eq!(out.stats.ledger_exits, 1);
    assert_eq!(out.stats.strategy_exits, 0);
    let closed = out.ledger.closed_trades();
    assert_eq!(closed[0].exit_reason, ExitReason::Stop);
    assert_eq!(closed[0].signal_reason, None);
    let open = out.ledger.open_positions();
    assert_eq!(open[0].direction, Direction::Short);
    assert_eq!(open[0].entry_bar_index, 5);
    // The stop fill is at its level, the entry at the close.
    let fills = out.ledger.fills();
    assert_ne!(fills[1].raw_price, fills[2].raw_price);
    assert_eq!(fills[2].raw_price, days[5].3);
    // The strategy left the long itself: it is not told the ledger closed it.
    assert!(
        !strategy
            .feedback
            .contains(&PositionFeedback::ClosedExternally)
    );
    assert_eq!(strategy.position(), Some(Direction::Short));
}

#[test]
fn a_reversal_while_flat_is_refused() {
    let refused = Err(EngineError::Bridge(BridgeError::OutOfStep(
        "reversal while flat",
    )));
    // Inside the window.
    let mut strategy = Scripted::new(&[(3, Step::Reverse(Direction::Short))]);
    let out = run(&mut strategy, &quiet(10), 0, EndOfRun::Close);
    assert_eq!(out.map(|o| o.stats), refused.clone());
    // In warm-up (the window opens on day 5).
    let mut strategy = Scripted::new(&[(2, Step::Reverse(Direction::Long))]);
    let out = run(&mut strategy, &quiet(10), 5, EndOfRun::Close);
    assert_eq!(out.map(|o| o.stats), refused);
}

#[test]
fn a_reversal_into_the_open_direction_is_refused() {
    let mut strategy = Scripted::new(&[
        (2, Step::Enter(Direction::Long)),
        (4, Step::Reverse(Direction::Long)),
    ]);
    let out = run(&mut strategy, &quiet(10), 0, EndOfRun::Close);
    assert_eq!(
        out.map(|o| o.stats),
        Err(EngineError::Bridge(BridgeError::OutOfStep(
            "reversal in the open position's direction"
        )))
    );
}

#[test]
fn an_entry_while_a_position_is_open_is_still_refused() {
    let mut strategy = Scripted::new(&[
        (2, Step::Enter(Direction::Long)),
        (4, Step::Enter(Direction::Short)),
    ]);
    let out = run(&mut strategy, &quiet(10), 0, EndOfRun::Close);
    assert_eq!(
        out.map(|o| o.stats),
        Err(EngineError::Bridge(BridgeError::OutOfStep(
            "entry while a position is open"
        )))
    );
}

#[test]
fn exit_then_enter_is_unchanged_two_bars_and_a_flat_close_between() {
    let mut strategy = Scripted::new(&[
        (2, Step::Enter(Direction::Long)),
        (5, Step::Exit),
        (6, Step::Enter(Direction::Short)),
    ]);
    let out = run(&mut strategy, &quiet(10), 0, EndOfRun::LeaveOpen).unwrap();

    assert_eq!(out.stats.reversals, 0);
    assert_eq!(out.stats.entries, 2);
    assert_eq!(out.stats.strategy_exits, 1);
    let closed = out.ledger.closed_trades();
    assert_eq!(closed.len(), 1);
    assert_eq!(closed[0].signal_reason, Some(SignalExitReason::StopLoss));
    let open = out.ledger.open_positions();
    assert_eq!(open[0].direction, Direction::Short);
    assert_eq!(open[0].entry_bar_index, 6);
    // The exit fills at the bar-5 close and the entry a bar later, at the bar-6 close.
    assert_eq!(closed[0].exit_ts + Duration::days(1), open[0].entry_ts);
    assert_eq!(out.ledger.fills().len(), 3);
}
