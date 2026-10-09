//! The bridge between a [`Strategy`](super::Strategy) and the [`Ledger`](crate::Ledger).
//!
//! The two sides use different units and types, and this module is the one place that
//! converts between them:
//!
//! | | Strategy (`strategy::`) | Ledger (`ledger::`, `exit::`, `cost::`) |
//! |---|---|---|
//! | Entry price | `Entry::reference_price`: raw units, the signal bar's close on the strategy's side | `Fill::fill_price`: executed price, **quote** units (raw × `raw_scale`, plus spread and slippage) |
//! | Stop / target | Raw units, anchored at `reference_price` | Raw units on the venue's configured price side (`OpenPosition::stop`/`target`) |
//! | Direction | [`crate::Direction`], one type shared by both sides | the same |
//! | Exit reason | [`SignalExitReason`](crate::SignalExitReason), the Python labels | Every strategy exit is booked as [`ExitReason::Signal`](crate::ExitReason::Signal) (`From<SignalExitReason>`) through [`Ledger::close_signal`](crate::Ledger::close_signal), which keeps the label as `signal_reason`; the ledger's own `Stop`/`Target` reach the strategy as [`PositionFeedback::ClosedExternally`] |
//!
//! **Anchoring an entry.** After the ledger books an entry, [`FillBridge::anchor`]
//! converts the executed fill back to raw units (`fill_price / raw_scale`) and shifts
//! the entry's stop and target by `raw_fill − reference_price`, which keeps their ATR
//! distances from the real fill (Python anchors its thresholds at the fill too). The
//! engine sends [`AnchoredEntry::feedback`] to the strategy and the shifted levels to
//! [`Ledger::set_levels`](crate::Ledger::set_levels). Later [`SignalKind::MoveStop`]
//! levels are computed by the strategy from the re-anchored entry and need no shift.
//!
//! **One close per position per bar.** [`resolve_bar`] fixes the order. On each bar
//! with a position open, the engine:
//! 1. evaluates the ledger's standing stop/target with
//!    [`Ledger::check_exit`](crate::Ledger::check_exit) (they trade during the bar,
//!    before its close is known);
//! 2. always calls [`Strategy::on_bar`](super::Strategy::on_bar) (indicators must see
//!    every bar);
//! 3. applies [`resolve_bar`]: a ledger trigger wins and the strategy's `Exit` or
//!    `MoveStop` for that bar is discarded. If the strategy did not itself exit, it is
//!    told [`PositionFeedback::ClosedExternally`] *after* its `on_bar`, which leaves it
//!    in exactly the state of a strategy exit on that bar (flat, cooldown from 0, no
//!    same-bar re-entry). Without a trigger, the strategy's `Exit` closes the position
//!    at the bar close and its `MoveStop` moves the ledger stop.
//!
//! Price sides: the ledger evaluates levels on the venue's configured side (mid for the
//! built-in venues) while a strategy reads its own side (bid by default). The levels keep
//! their distance from the fill either way; the two evaluators may see prices half a
//! Dukascopy spread apart, and [`resolve_bar`] keeps that from ever closing a position
//! twice.

use super::{Direction, Entry, PositionFeedback, Signal, SignalExitReason, SignalKind};
use crate::cost::config::PriceSide;
use crate::cost::fill::{FillModel, TradeSide};
use crate::exit::ExitTrigger;
use crate::ledger::{Fill, OpenOrder};

/// Why the bridge refused a conversion or a bar.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum BridgeError {
    /// The instrument's `raw_scale` is not finite and positive.
    #[error("raw_scale {0} must be finite and > 0")]
    RawScale(f64),
    /// The fill given to [`FillBridge::anchor`] is not this entry's opening fill.
    #[error("not the entry fill: {0}")]
    NotTheEntryFill(&'static str),
    /// The ledger and strategy disagree about whether a position is open.
    #[error("strategy/ledger out of step: {0}")]
    OutOfStep(&'static str),
}

/// Unit conversion for one instrument at one venue.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct FillBridge {
    raw_scale: f64,
    venue_side: PriceSide,
}

/// An entry re-anchored at its executed fill.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct AnchoredEntry {
    /// The executed fill price in raw units (`fill_price / raw_scale`).
    pub raw_fill: f64,
    /// The entry's stop shifted by `raw_fill − reference_price`.
    pub stop: Option<f64>,
    /// The entry's target shifted by `raw_fill − reference_price`.
    pub target: Option<f64>,
}

impl AnchoredEntry {
    /// The feedback that re-anchors the strategy's signalled position.
    #[must_use]
    pub fn feedback(&self) -> PositionFeedback {
        PositionFeedback::EntryFilled {
            price: self.raw_fill,
        }
    }
}

impl FillBridge {
    /// A bridge for the instrument `model` prices.
    ///
    /// # Errors
    /// [`BridgeError::RawScale`] when the instrument's `raw_scale` is not finite and
    /// positive.
    pub fn new(model: &FillModel) -> Result<Self, BridgeError> {
        let raw_scale = model.spec().raw_scale;
        if !(raw_scale.is_finite() && raw_scale > 0.0) {
            return Err(BridgeError::RawScale(raw_scale));
        }
        Ok(Self {
            raw_scale,
            venue_side: model.price_side(),
        })
    }

    /// The venue's configured price side, on which the ledger evaluates levels.
    #[must_use]
    pub fn venue_side(&self) -> PriceSide {
        self.venue_side
    }

    /// A quote-unit price in raw units.
    #[must_use]
    pub fn to_raw(&self, quote: f64) -> f64 {
        quote / self.raw_scale
    }

    /// The ledger order for `entry`, levels as signalled (anchored at the reference
    /// price; [`FillBridge::anchor`] moves them once the fill is known).
    #[must_use]
    pub fn open_order(&self, instrument: &str, entry: &Entry, size: f64) -> OpenOrder {
        OpenOrder {
            instrument: instrument.to_owned(),
            direction: entry.direction,
            size,
            stop: entry.stop,
            target: entry.target,
        }
    }

    /// Re-anchor `entry` at the executed `fill` the ledger booked for it.
    ///
    /// # Errors
    /// [`BridgeError::NotTheEntryFill`] for a closing fill, a fill on the wrong side, or
    /// a fill price that is not finite and positive.
    pub fn anchor(&self, entry: &Entry, fill: &Fill) -> Result<AnchoredEntry, BridgeError> {
        if fill.exit_reason.is_some() {
            return Err(BridgeError::NotTheEntryFill("a closing fill"));
        }
        let expected = match entry.direction {
            Direction::Long => TradeSide::Buy,
            Direction::Short => TradeSide::Sell,
        };
        if fill.side != expected {
            return Err(BridgeError::NotTheEntryFill("trade side does not match"));
        }
        if !(fill.fill_price.is_finite() && fill.fill_price > 0.0) {
            return Err(BridgeError::NotTheEntryFill(
                "fill price not finite and > 0",
            ));
        }
        let raw_fill = self.to_raw(fill.fill_price);
        let shift = raw_fill - entry.reference_price;
        Ok(AnchoredEntry {
            raw_fill,
            stop: entry.stop.map(|s| s + shift),
            target: entry.target.map(|t| t + shift),
        })
    }
}

/// What the engine does on one bar, from [`resolve_bar`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum BarAction {
    /// Nothing to book.
    None,
    /// Open a position for this entry (the engine prices it and then anchors it).
    Enter(Entry),
    /// Close at the ledger's trigger. When `notify_strategy` is set, send
    /// [`PositionFeedback::ClosedExternally`] after booking it.
    LedgerExit {
        /// The triggered stop or target.
        trigger: ExitTrigger,
        /// The strategy still holds the position and must be told.
        notify_strategy: bool,
    },
    /// Close at the bar close for the strategy's reason (booked as
    /// [`crate::ExitReason::Signal`] by [`crate::Ledger::close_signal`]).
    StrategyExit {
        /// The strategy's reason.
        reason: SignalExitReason,
    },
    /// Move the ledger stop.
    MoveStop {
        /// The new stop, raw units, already anchored at the fill.
        stop: f64,
    },
}

/// Decide one bar given whether a position was open before it, the ledger's
/// [`Ledger::check_exit`](crate::Ledger::check_exit) result, and the strategy's signal
/// for the same bar. See the module docs for the order.
///
/// # Errors
/// [`BridgeError::OutOfStep`] when the inputs contradict each other: a trigger or a
/// strategy exit/stop move with no position open, or an entry while one is open.
pub fn resolve_bar(
    position_open: bool,
    trigger: Option<ExitTrigger>,
    signal: Option<&Signal>,
) -> Result<BarAction, BridgeError> {
    let kind = signal.map(|s| s.kind);
    if !position_open {
        return match (trigger, kind) {
            (Some(_), _) => Err(BridgeError::OutOfStep("ledger trigger with no position")),
            (None, Some(SignalKind::Enter(entry))) => Ok(BarAction::Enter(entry)),
            (None, Some(_)) => Err(BridgeError::OutOfStep("exit or stop move while flat")),
            (None, None) => Ok(BarAction::None),
        };
    }
    match (trigger, kind) {
        (_, Some(SignalKind::Enter(_))) => {
            Err(BridgeError::OutOfStep("entry while a position is open"))
        }
        (Some(trigger), kind) => Ok(BarAction::LedgerExit {
            trigger,
            notify_strategy: !matches!(kind, Some(SignalKind::Exit { .. })),
        }),
        (None, Some(SignalKind::Exit { reason })) => Ok(BarAction::StrategyExit { reason }),
        (None, Some(SignalKind::MoveStop { stop })) => Ok(BarAction::MoveStop { stop }),
        (None, None) => Ok(BarAction::None),
    }
}

#[cfg(test)]
mod tests {
    use chrono::{DateTime, Duration, TimeZone, Utc};

    use super::super::Strategy;
    use super::*;
    use crate::cost::config::CostConfig;
    use crate::cost::fill::PricePoint;
    use crate::exit::{BothHit, ExitEvaluation};
    use crate::ledger::{AccountFx, FillAt, Ledger, PositionId};
    use crate::toy::{ThresholdConfig, ThresholdCross};
    use crate::{BarTimeframe, JoinedBar, JoinedSeries, Ohlcv};

    const SYMBOL: &str = "XAUUSD"; // raw_scale 0.01: the conversion is not the identity
    const CENTS: f64 = 2000.0; // scales the unit-sized test path to ~$2000 gold in cents

    fn start() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, 1, 0, 0, 0).unwrap()
    }

    /// Daily bars, bid = ask, from (open, high, low, close) in path units.
    fn series(bars: &[(f64, f64, f64, f64)]) -> JoinedSeries {
        let bars = bars
            .iter()
            .zip(0..)
            .map(|(&(o, h, l, c), i)| {
                let side = Ohlcv {
                    open: o * CENTS,
                    high: h * CENTS,
                    low: l * CENTS,
                    close: c * CENTS,
                    tick_volume: 1.0,
                };
                JoinedBar {
                    ts_open_utc: start() + Duration::days(i),
                    bid: side,
                    ask: side,
                }
            })
            .collect();
        JoinedSeries::new(SYMBOL, BarTimeframe::D1, bars).unwrap()
    }

    /// 60 quiet bars alternating 100 / 100.3, a breakout bar closing at 103 (the test
    /// strategy's entry: it crosses 101.5), then `after`.
    fn path(after: &[(f64, f64, f64, f64)]) -> JoinedSeries {
        let mut bars = Vec::new();
        let mut prev = 100.0;
        for i in 0..60_u8 {
            let c = if i % 2 == 1 { 100.3 } else { 100.0 };
            bars.push((prev, f64::max(prev, c) + 0.5, f64::min(prev, c) - 0.5, c));
            prev = c;
        }
        bars.push((prev, 103.5, prev - 0.5, 103.0));
        bars.extend_from_slice(after);
        series(&bars)
    }

    /// Enters on a cross of 101.5 with the stop 2.5 under the entry close, and exits on
    /// a close under 101.5 (path units).
    fn strategy() -> ThresholdCross {
        ThresholdCross::new(ThresholdConfig {
            enter_above: 101.5 * CENTS,
            exit_below: 101.5 * CENTS,
            stop_distance: 2.5 * CENTS,
            target_distance: 7.5 * CENTS,
            ..ThresholdConfig::default()
        })
        .unwrap()
    }

    fn ledger(eval: ExitEvaluation) -> Ledger {
        let fx = AccountFx::new("GBP", [("USD".to_owned(), 0.8)]).unwrap();
        Ledger::new(&CostConfig::builtin(), "ig_spread_bet", eval, fx).unwrap()
    }

    /// What happened, for the assertions.
    #[derive(Debug, Default)]
    struct Run {
        entry: Option<Entry>,
        anchored: Option<AnchoredEntry>,
        actions: Vec<(usize, BarAction)>,
        notified: Vec<usize>,
    }

    /// The engine loop the module docs describe: ledger check, strategy bar, resolve.
    fn drive(series: &JoinedSeries, eval: ExitEvaluation) -> (Ledger, ThresholdCross, Run) {
        let mut ledger = ledger(eval);
        let mut strategy = strategy();
        let bridge = FillBridge::new(ledger.fill_model(SYMBOL).unwrap()).unwrap();
        let mut run = Run::default();
        let mut open: Option<PositionId> = None;
        for (i, bar) in series.bars().iter().enumerate() {
            let trigger = match open {
                Some(id) => ledger.check_exit(id, bar).unwrap(),
                None => None,
            };
            let signal = strategy.on_bar(bar).unwrap();
            let action = resolve_bar(open.is_some(), trigger, signal.as_ref()).unwrap();
            let at = |point| FillAt {
                bar,
                point,
                ts: bar.ts_close_utc(BarTimeframe::D1),
                bar_index: i,
            };
            match action {
                BarAction::None => continue,
                BarAction::Enter(entry) => {
                    let order = bridge.open_order(SYMBOL, &entry, 1.0);
                    let id = ledger.open(&order, at(PricePoint::Close)).unwrap();
                    let anchored = bridge
                        .anchor(&entry, ledger.fills().last().unwrap())
                        .unwrap();
                    strategy.on_feedback(anchored.feedback()).unwrap();
                    ledger
                        .set_levels(id, anchored.stop, anchored.target)
                        .unwrap();
                    open = Some(id);
                    run.entry = Some(entry);
                    run.anchored = Some(anchored);
                }
                BarAction::LedgerExit {
                    trigger,
                    notify_strategy,
                } => {
                    ledger
                        .close(open.take().unwrap(), at(trigger.at), trigger.reason, series)
                        .unwrap();
                    if notify_strategy {
                        strategy
                            .on_feedback(PositionFeedback::ClosedExternally)
                            .unwrap();
                        run.notified.push(i);
                    }
                }
                BarAction::StrategyExit { reason } => {
                    ledger
                        .close_signal(open.take().unwrap(), at(PricePoint::Close), reason, series)
                        .unwrap();
                }
                BarAction::MoveStop { stop } => {
                    let id = open.unwrap();
                    let target = ledger.open_positions()[0].target;
                    ledger.set_levels(id, Some(stop), target).unwrap();
                }
            }
            run.actions.push((i, action));
            // The two books never disagree after a bar.
            assert_eq!(open.is_some(), strategy.position().is_some(), "bar {i}");
            assert_eq!(
                open.is_some(),
                !ledger.open_positions().is_empty(),
                "bar {i}"
            );
        }
        (ledger, strategy, run)
    }

    const INTRABAR: ExitEvaluation = ExitEvaluation::Intrabar {
        both_hit: BothHit::StopFirst,
    };

    #[test]
    fn entry_fill_is_converted_to_raw_and_levels_shift_with_it() {
        let s = path(&[(103.0, 103.2, 102.6, 102.9)]);
        let (ledger, _, run) = drive(&s, INTRABAR);
        let entry = run.entry.expect("the breakout enters");
        let anchored = run.anchored.unwrap();
        let fill = &ledger.fills()[0];
        // Quote fill = mid close × 0.01 + half spread + slippage, so above the reference.
        assert!(fill.fill_price > entry.reference_price * 0.01);
        assert!((anchored.raw_fill * 0.01 - fill.fill_price).abs() < 1e-9);
        let shift = anchored.raw_fill - entry.reference_price;
        assert!(shift > 0.0);
        let pos = &ledger.open_positions()[0];
        assert_eq!(pos.stop, entry.stop.map(|x| x + shift));
        assert_eq!(pos.target, entry.target.map(|x| x + shift));
        assert_eq!(pos.stop, anchored.stop);
    }

    #[test]
    fn ledger_intrabar_stop_closes_once_and_the_strategy_is_told() {
        // Bar 61 dips through the stop intrabar but closes above both the stop and the
        // exit level: only the ledger exits, and the strategy learns of it afterwards.
        let s = path(&[(103.0, 103.2, 97.0, 102.5), (102.5, 102.8, 102.2, 102.6)]);
        let (ledger, strategy, run) = drive(&s, INTRABAR);
        assert_eq!(ledger.closed_trades().len(), 1);
        assert_eq!(ledger.fills().len(), 2);
        let trade = &ledger.closed_trades()[0];
        assert_eq!(trade.exit_reason, crate::ExitReason::Stop);
        assert_eq!(run.notified, vec![61]);
        assert!(matches!(
            run.actions.last(),
            Some((
                61,
                BarAction::LedgerExit {
                    notify_strategy: true,
                    ..
                }
            ))
        ));
        assert_eq!(strategy.position(), None);
    }

    #[test]
    fn strategy_close_exit_closes_once_at_the_close() {
        // Bar 61 closes under the exit level but above the stop: close-only, the ledger
        // sees nothing and the strategy's exit is booked as a signal exit.
        let s = path(&[(103.0, 103.0, 100.8, 101.0)]);
        let (ledger, _, run) = drive(&s, ExitEvaluation::CloseOnly);
        assert_eq!(
            run.actions.last(),
            Some(&(
                61,
                BarAction::StrategyExit {
                    reason: SignalExitReason::StopLoss
                }
            ))
        );
        assert_eq!(ledger.closed_trades().len(), 1);
        assert_eq!(
            ledger.closed_trades()[0].exit_reason,
            crate::ExitReason::Signal
        );
        assert_eq!(
            ledger.closed_trades()[0].signal_reason,
            Some(SignalExitReason::StopLoss)
        );
        assert_eq!(
            ledger.fills()[1].signal_reason,
            Some(SignalExitReason::StopLoss)
        );
        assert!(run.notified.is_empty());
    }

    #[test]
    fn both_on_one_bar_the_ledger_wins_and_the_strategy_is_not_told_twice() {
        // Bar 61 closes far below the stop: the ledger (close-only) and the strategy's
        // close-based stop both fire. One close, booked as the ledger's stop; the
        // strategy already exited, so no feedback is sent.
        let s = path(&[(103.0, 103.0, 97.5, 98.0), (98.0, 98.4, 97.8, 98.2)]);
        let (ledger, strategy, run) = drive(&s, ExitEvaluation::CloseOnly);
        assert_eq!(ledger.closed_trades().len(), 1);
        assert_eq!(ledger.fills().len(), 2);
        assert_eq!(
            ledger.closed_trades()[0].exit_reason,
            crate::ExitReason::Stop
        );
        assert!(matches!(
            run.actions.last(),
            Some((
                61,
                BarAction::LedgerExit {
                    notify_strategy: false,
                    ..
                }
            ))
        ));
        assert!(run.notified.is_empty());
        assert_eq!(strategy.position(), None);
    }

    #[test]
    fn resolve_bar_rejects_out_of_step_inputs() {
        let trigger = ExitTrigger {
            reason: crate::ExitReason::Stop,
            at: PricePoint::Close,
        };
        let signal = |kind| Signal {
            ts_open_utc: start(),
            kind,
        };
        let enter = signal(SignalKind::Enter(Entry {
            direction: Direction::Long,
            reference_price: 1.0,
            stop: None,
            target: None,
            atr: 1.0,
        }));
        let exit = signal(SignalKind::Exit {
            reason: SignalExitReason::StopLoss,
        });
        assert!(resolve_bar(false, Some(trigger), None).is_err());
        assert!(resolve_bar(false, None, Some(&exit)).is_err());
        assert!(resolve_bar(true, None, Some(&enter)).is_err());
        assert_eq!(resolve_bar(false, None, None), Ok(BarAction::None));
        let moved = signal(SignalKind::MoveStop { stop: 5.0 });
        assert_eq!(
            resolve_bar(true, Some(trigger), Some(&moved)),
            Ok(BarAction::LedgerExit {
                trigger,
                notify_strategy: true
            })
        );
    }

    #[test]
    fn anchor_refuses_a_closing_or_wrong_side_fill() {
        let s = path(&[(103.0, 103.0, 97.5, 98.0)]);
        let (ledger, _, run) = drive(&s, ExitEvaluation::CloseOnly);
        let bridge = FillBridge::new(ledger.fill_model(SYMBOL).unwrap()).unwrap();
        let entry = run.entry.unwrap();
        assert!(
            bridge.anchor(&entry, &ledger.fills()[1]).is_err(),
            "closing fill"
        );
        let short = Entry {
            direction: Direction::Short,
            ..entry
        };
        assert!(
            bridge.anchor(&short, &ledger.fills()[0]).is_err(),
            "buy fill"
        );
        assert_eq!(bridge.venue_side(), PriceSide::Mid);
    }
}
