//! Strategies: a [`Strategy`] consumes bars and emits typed [`Signal`]s.
//!
//! A strategy **does not fill orders and computes no P&L**. Fills, venue costs and
//! the ledger are the engine's, metrics are [`crate::metrics`]'. What a strategy does own
//! is the position it has *signalled*: it assumes every [`SignalKind::Enter`] fills at the
//! signal bar's close (`reference_price`) and every [`SignalKind::Exit`] flattens, and it
//! evaluates its own close-based exits.
//!
//! No strategy ships in this crate. A strategy crate implements [`Strategy`], and
//! registers a factory for it in a [`crate::sweep::StrategyRegistry`] to run it in
//! sweeps and from the command line.
//!
//! The engine corrects that assumption through [`Strategy::on_feedback`] with a
//! [`PositionFeedback`] (a type defined here, so strategies never depend on the
//! engine's types):
//! - [`PositionFeedback::EntryFilled`] re-anchors the entry at the real fill price
//!   (Python anchors its stop arithmetic at the fill);
//! - [`PositionFeedback::EntryRejected`] reverts the strategy to flat and restarts its
//!   cooldown exactly as an exit would, so a refused entry is not re-signalled on the
//!   next bar regardless of the cooldown;
//! - [`PositionFeedback::ClosedExternally`] reports an intrabar stop or target the
//!   engine took; the strategy flattens and starts its cooldown.
//!
//! Entry signals carry their stop and target as price levels (raw instrument units,
//! anchored at `reference_price`) so the engine can evaluate them intrabar. An engine
//! that fills at a different price shifts them by `fill - reference_price`, which is
//! what the strategy itself does after `EntryFilled`. The [`bridge`](FillBridge)
//! helpers do that conversion against the [`crate::Ledger`] (whose fills are in quote
//! units) and fix the per-bar order between the ledger's stop/target check and the
//! strategy's close-based exits, so a position is never closed twice.
//!
//! Bars reach a strategy through [`Strategy::on_context_bar`] (higher timeframes it
//! declares) and [`Strategy::on_bar`] (its primary timeframe). [`run_signals`] is the
//! signal-only replay (for signal goldens and parity checks): it delivers each
//! context bar before the first primary bar that closes at or after it, and assumes
//! every signal is acted on. [`BarFeed`] is the same merge one bar at a time, for a
//! caller that sends feedback between bars.

mod bridge;
mod sizing;

pub use bridge::{AnchoredEntry, BarAction, BridgeError, FillBridge, resolve_bar};
pub use sizing::{SizingConfig, atr_normalised_size};

use std::cmp::Reverse;

use chrono::{DateTime, Utc};

use crate::indicators::IndicatorError;
use crate::{BarTimeframe, JoinedBar, JoinedSeries};

/// Position direction: the crate's one `Direction` ([`crate::Direction`]), re-exported
/// here so strategies and the ledger share it.
pub use crate::cost::financing::Direction;
/// Why a strategy exits ([`crate::SignalExitReason`]); the ledger books it as
/// [`crate::ExitReason::Signal`] and keeps the label.
pub use crate::exit::SignalExitReason;

/// An entry request.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Entry {
    /// Long or short.
    pub direction: Direction,
    /// The signal bar's close on the strategy's price side: the price the strategy
    /// assumes until told otherwise.
    pub reference_price: f64,
    /// Protective stop level, if the strategy has one.
    pub stop: Option<f64>,
    /// Profit target level, if the strategy has one.
    pub target: Option<f64>,
    /// The ATR the levels were sized from (raw price units).
    pub atr: f64,
}

/// What a strategy asks for on one bar.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum SignalKind {
    /// Open a position.
    Enter(Entry),
    /// Move the open position's protective stop to this level (breakeven ratchet).
    MoveStop {
        /// The new stop level.
        stop: f64,
    },
    /// Close the open position.
    Exit {
        /// Why.
        reason: SignalExitReason,
    },
}

/// One signal, labelled with the open of the primary bar it fired on (the label the
/// Python strategies see).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Signal {
    /// Open of the primary bar whose close produced the signal.
    pub ts_open_utc: DateTime<Utc>,
    /// The request.
    pub kind: SignalKind,
}

/// The engine's report back to a strategy about its signalled position.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum PositionFeedback {
    /// The last entry filled at `price`; re-anchor the position there.
    EntryFilled {
        /// Executed fill price in **raw** units (the ledger's `Fill::fill_price` is in
        /// quote units: convert with [`FillBridge::anchor`]).
        price: f64,
    },
    /// The last entry was not filled (the engine sends it for an entry signalled during
    /// warm-up). The strategy is flat again and **restarts its cooldown exactly as a
    /// filled-then-exited position would**, so it does not re-signal the same entry on
    /// every bar. A strategy with no cooldown of its own should wait a default of its own
    /// instead.
    EntryRejected,
    /// The engine closed the position itself (a ledger stop or target). Send it after
    /// the strategy's [`Strategy::on_bar`] for the bar the ledger closed on, and only if
    /// that bar's signal was not itself an exit (see [`resolve_bar`]).
    ClosedExternally,
}

/// Why a strategy could not be built.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum ConfigError {
    /// A parameter is out of range.
    #[error("invalid {field}: {reason}")]
    Invalid {
        /// Config field.
        field: &'static str,
        /// What is wrong with it.
        reason: &'static str,
    },
    /// An indicator rejected its parameters.
    #[error("indicator parameters: {0}")]
    Indicator(#[from] IndicatorError),
}

/// Why a strategy could not process a bar or a feedback message.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum StrategyError {
    /// The bar failed price validation (RAD-1906).
    #[error("bar at {ts}: {source}")]
    BadBar {
        /// Open of the rejected bar.
        ts: DateTime<Utc>,
        /// The validation failure.
        #[source]
        source: IndicatorError,
    },
    /// A context bar arrived for a timeframe the strategy did not declare.
    #[error("unexpected {timeframe} context bar")]
    UnexpectedTimeframe {
        /// The offending timeframe.
        timeframe: BarTimeframe,
    },
    /// Feedback that does not fit the strategy's position state.
    #[error("invalid position feedback: {reason}")]
    Feedback {
        /// What is wrong.
        reason: &'static str,
    },
}

/// A bar-driven strategy that emits signals and never fills them.
pub trait Strategy {
    /// Stable name, e.g. `"threshold_cross"`.
    fn name(&self) -> &'static str;

    /// The timeframe [`Strategy::on_bar`] expects.
    fn primary_timeframe(&self) -> BarTimeframe;

    /// Higher timeframes the strategy reads as context, if any.
    fn context_timeframes(&self) -> &[BarTimeframe] {
        &[]
    }

    /// The quote units per raw price unit that the strategy's own signals assume, when
    /// any signal depends on it (for example a notional cap computed in quote units, from
    /// [`SizingConfig::raw_scale`]).
    /// [`crate::engine::run`] refuses a strategy whose scale differs from the
    /// instrument's. `None` (the default) when no signal depends on the scale.
    fn price_scale(&self) -> Option<f64> {
        None
    }

    /// Fold one completed context bar. The caller delivers it before the first
    /// primary bar whose close is at or after the context bar's close.
    ///
    /// # Errors
    /// [`StrategyError::UnexpectedTimeframe`] for an undeclared timeframe (the
    /// default), [`StrategyError::BadBar`] for a corrupt bar.
    fn on_context_bar(
        &mut self,
        timeframe: BarTimeframe,
        bar: &JoinedBar,
    ) -> Result<(), StrategyError> {
        let _ = bar;
        Err(StrategyError::UnexpectedTimeframe { timeframe })
    }

    /// Process one completed primary bar and return at most one signal.
    ///
    /// # Errors
    /// [`StrategyError::BadBar`] for a corrupt bar; the strategy's state is unchanged.
    fn on_bar(&mut self, bar: &JoinedBar) -> Result<Option<Signal>, StrategyError>;

    /// Correct the signalled position with what the engine actually did.
    ///
    /// # Errors
    /// [`StrategyError::Feedback`] when there is no position to apply it to, or the
    /// fill price is not a finite positive number.
    fn on_feedback(&mut self, feedback: PositionFeedback) -> Result<(), StrategyError>;

    /// Direction of the signalled position, `None` when flat.
    fn position(&self) -> Option<Direction>;
}

/// Why [`run_signals`] stopped.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum RunError {
    /// The primary series is not at the strategy's primary timeframe.
    #[error("primary series is {got}, strategy expects {expected}")]
    PrimaryTimeframe {
        /// The strategy's primary timeframe.
        expected: BarTimeframe,
        /// The series' timeframe.
        got: BarTimeframe,
    },
    /// A declared context timeframe has no series, or a series was given twice.
    #[error("context series for {timeframe}: {reason}")]
    Context {
        /// The timeframe.
        timeframe: BarTimeframe,
        /// What is wrong.
        reason: &'static str,
    },
    /// The strategy rejected a bar.
    #[error(transparent)]
    Strategy(#[from] StrategyError),
}

/// Steps a strategy through a primary series and its context series one primary bar
/// at a time, so a caller (the engine, a test) can send [`PositionFeedback`] between
/// bars. [`run_signals`] is this loop with no feedback.
///
/// Delivery order: before each primary bar, every context bar whose close (open +
/// timeframe) is at or before the primary bar's close, ordered by close and, on a tie,
/// the longer timeframe first. A context bar therefore never reaches the strategy
/// before it has closed. The Python fixture driver uses the same rule.
#[derive(Debug, Clone)]
pub struct BarFeed<'a> {
    primary: &'a JoinedSeries,
    queue: Vec<(DateTime<Utc>, Reverse<BarTimeframe>, &'a JoinedBar)>,
    next_context: usize,
    next_primary: usize,
}

impl<'a> BarFeed<'a> {
    /// Check the series against what `strategy` declares and prepare the merge.
    ///
    /// # Errors
    /// [`RunError::PrimaryTimeframe`] or [`RunError::Context`] when the series do not
    /// match the strategy's primary and context timeframes exactly.
    pub fn new<S: Strategy + ?Sized>(
        strategy: &S,
        primary: &'a JoinedSeries,
        contexts: &[&'a JoinedSeries],
    ) -> Result<Self, RunError> {
        let primary_tf = strategy.primary_timeframe();
        if primary.timeframe() != primary_tf {
            return Err(RunError::PrimaryTimeframe {
                expected: primary_tf,
                got: primary.timeframe(),
            });
        }
        let declared = strategy.context_timeframes();
        for series in contexts {
            let tf = series.timeframe();
            if !declared.contains(&tf) {
                return Err(RunError::Context {
                    timeframe: tf,
                    reason: "not declared by the strategy",
                });
            }
            if contexts.iter().filter(|s| s.timeframe() == tf).count() > 1 {
                return Err(RunError::Context {
                    timeframe: tf,
                    reason: "given more than once",
                });
            }
        }
        if let Some(&tf) = declared
            .iter()
            .find(|tf| !contexts.iter().any(|s| s.timeframe() == **tf))
        {
            return Err(RunError::Context {
                timeframe: tf,
                reason: "missing",
            });
        }

        let mut queue: Vec<_> = contexts
            .iter()
            .flat_map(|s| {
                let tf = s.timeframe();
                s.bars()
                    .iter()
                    .map(move |b| (b.ts_close_utc(tf), Reverse(tf), b))
            })
            .collect();
        queue.sort_by_key(|(close, tf, _)| (*close, *tf));
        Ok(Self {
            primary,
            queue,
            next_context: 0,
            next_primary: 0,
        })
    }

    /// Deliver the context bars due before the next primary bar, then the primary bar.
    /// Returns that bar and its signal, or `None` once the primary series is exhausted.
    ///
    /// # Errors
    /// [`RunError::Strategy`] when the strategy rejects a bar.
    pub fn step<S: Strategy + ?Sized>(
        &mut self,
        strategy: &mut S,
    ) -> Result<Option<(&'a JoinedBar, Option<Signal>)>, RunError> {
        let Some(bar) = self.primary.bars().get(self.next_primary) else {
            return Ok(None);
        };
        let close = bar.ts_close_utc(self.primary.timeframe());
        while let Some(&(ctx_close, Reverse(tf), ctx)) = self.queue.get(self.next_context) {
            if ctx_close > close {
                break;
            }
            strategy.on_context_bar(tf, ctx)?;
            self.next_context += 1;
        }
        let signal = strategy.on_bar(bar)?;
        self.next_primary += 1;
        Ok(Some((bar, signal)))
    }
}

/// Replay `primary` (and the declared `contexts`) through `strategy` with a
/// [`BarFeed`], collecting every signal and assuming each one is acted on (no
/// feedback is sent).
///
/// # Errors
/// [`RunError`] for a timeframe mismatch, a missing or duplicated context series, or
/// a bar the strategy rejects.
pub fn run_signals<S: Strategy + ?Sized>(
    strategy: &mut S,
    primary: &JoinedSeries,
    contexts: &[&JoinedSeries],
) -> Result<Vec<Signal>, RunError> {
    let mut feed = BarFeed::new(strategy, primary, contexts)?;
    let mut signals = Vec::new();
    while let Some((_, signal)) = feed.step(strategy)? {
        signals.extend(signal);
    }
    Ok(signals)
}

/// Validate the configured side of `bar` (RAD-1906) before any state changes: the
/// check every strategy runs on a bar before its indicators see it.
///
/// # Errors
/// [`StrategyError::BadBar`] when the side's prices fail [`crate::indicators::check_bar`].
pub fn checked_side(
    bar: &JoinedBar,
    side: tradedesk_data::Side,
) -> Result<crate::Ohlcv, StrategyError> {
    let ohlcv = *bar.side(side);
    crate::indicators::check_bar(&ohlcv).map_err(|source| StrategyError::BadBar {
        ts: bar.ts_open_utc,
        source,
    })?;
    Ok(ohlcv)
}

/// Validate a fill price from [`PositionFeedback::EntryFilled`].
///
/// # Errors
/// [`StrategyError::Feedback`] unless the price is finite and positive.
pub fn checked_fill(price: f64) -> Result<f64, StrategyError> {
    if price.is_finite() && price > 0.0 {
        Ok(price)
    } else {
        Err(StrategyError::Feedback {
            reason: "fill price must be finite and positive",
        })
    }
}

/// Construct-time check for a strictly positive, finite multiplier.
///
/// # Errors
/// [`ConfigError::Invalid`] naming `field` otherwise.
pub fn positive(field: &'static str, value: f64) -> Result<f64, ConfigError> {
    if value.is_finite() && value > 0.0 {
        Ok(value)
    } else {
        Err(ConfigError::Invalid {
            field,
            reason: "must be finite and > 0",
        })
    }
}

/// Construct-time check for a finite, non-negative value.
///
/// # Errors
/// [`ConfigError::Invalid`] naming `field` otherwise.
pub fn non_negative(field: &'static str, value: f64) -> Result<f64, ConfigError> {
    if value.is_finite() && value >= 0.0 {
        Ok(value)
    } else {
        Err(ConfigError::Invalid {
            field,
            reason: "must be finite and >= 0",
        })
    }
}

#[cfg(test)]
pub(crate) mod test_support {
    use chrono::{DateTime, TimeZone, Utc};

    use crate::{BarTimeframe, JoinedBar, JoinedSeries, Ohlcv};

    /// A bar with the given high, low and close on both sides (open = close).
    pub(crate) fn bar(ts: DateTime<Utc>, high: f64, low: f64, close: f64) -> JoinedBar {
        let side = Ohlcv {
            open: close,
            high,
            low,
            close,
            tick_volume: 1.0,
        };
        JoinedBar {
            ts_open_utc: ts,
            bid: side,
            ask: side,
        }
    }

    /// Consecutive bars from `start`, one per `tf`, from (high, low, close) triples.
    pub(crate) fn series(
        tf: BarTimeframe,
        start: DateTime<Utc>,
        hlc: &[(f64, f64, f64)],
    ) -> JoinedSeries {
        let step = tf.duration();
        let bars = hlc
            .iter()
            .zip(0..)
            .map(|(&(h, l, c), i)| bar(start + step * i, h, l, c))
            .collect();
        JoinedSeries::new("TEST", tf, bars).unwrap()
    }

    /// Monday 2024-01-01 00:00Z.
    pub(crate) fn monday() -> DateTime<Utc> {
        Utc.with_ymd_and_hms(2024, 1, 1, 0, 0, 0).unwrap()
    }
}

#[cfg(test)]
mod tests {
    use super::test_support::{monday, series};
    use super::*;

    /// Records the order in which bars arrive.
    struct Recorder {
        seen: Vec<(BarTimeframe, DateTime<Utc>)>,
        contexts: Vec<BarTimeframe>,
    }

    impl Strategy for Recorder {
        fn name(&self) -> &'static str {
            "recorder"
        }
        fn primary_timeframe(&self) -> BarTimeframe {
            BarTimeframe::M15
        }
        fn context_timeframes(&self) -> &[BarTimeframe] {
            &self.contexts
        }
        fn on_context_bar(
            &mut self,
            tf: BarTimeframe,
            bar: &JoinedBar,
        ) -> Result<(), StrategyError> {
            self.seen.push((tf, bar.ts_open_utc));
            Ok(())
        }
        fn on_bar(&mut self, bar: &JoinedBar) -> Result<Option<Signal>, StrategyError> {
            self.seen.push((BarTimeframe::M15, bar.ts_open_utc));
            Ok(None)
        }
        fn on_feedback(&mut self, _: PositionFeedback) -> Result<(), StrategyError> {
            Ok(())
        }
        fn position(&self) -> Option<Direction> {
            None
        }
    }

    #[test]
    fn context_bars_arrive_after_they_close() {
        let t0 = monday();
        let flat = [(2.0, 1.0, 1.5); 8];
        let m15 = series(BarTimeframe::M15, t0, &flat);
        let h1 = series(BarTimeframe::H1, t0, &flat[..2]);
        let d1 = series(BarTimeframe::D1, t0 - chrono::Duration::days(1), &flat[..1]);
        let mut rec = Recorder {
            seen: vec![],
            contexts: vec![BarTimeframe::H1, BarTimeframe::D1],
        };
        run_signals(&mut rec, &m15, &[&h1, &d1]).unwrap();
        let m = |mins: i64| t0 + chrono::Duration::minutes(mins);
        assert_eq!(
            rec.seen,
            vec![
                // Yesterday's daily bar closed at t0: before the first 15m bar.
                (BarTimeframe::D1, t0 - chrono::Duration::days(1)),
                (BarTimeframe::M15, m(0)),
                (BarTimeframe::M15, m(15)),
                (BarTimeframe::M15, m(30)),
                // The 00:00 hour closes at 01:00, with the 00:45 bar: delivered first.
                (BarTimeframe::H1, m(0)),
                (BarTimeframe::M15, m(45)),
                (BarTimeframe::M15, m(60)),
                (BarTimeframe::M15, m(75)),
                (BarTimeframe::M15, m(90)),
                (BarTimeframe::H1, m(60)),
                (BarTimeframe::M15, m(105)),
            ]
        );
    }

    #[test]
    fn equal_closes_deliver_the_daily_bar_before_the_hourly_bar() {
        // The 23:00 hour and the day both close at 00:00 the next day, together with
        // the 23:45 15m bar: a genuine three-way tie on close. D1 must come first,
        // then H1, then the primary bar.
        let t0 = monday();
        let flat = [(2.0, 1.0, 1.5); 1];
        let at = |h: i64, m: i64| t0 + chrono::Duration::hours(h) + chrono::Duration::minutes(m);
        let m15 = series(BarTimeframe::M15, at(23, 45), &flat);
        let h1 = series(BarTimeframe::H1, at(23, 0), &flat);
        let d1 = series(BarTimeframe::D1, t0, &flat);
        assert_eq!(
            h1.bars()[0].ts_close_utc(BarTimeframe::H1),
            d1.bars()[0].ts_close_utc(BarTimeframe::D1)
        );
        // Given in either order, the delivery is the same.
        for contexts in [[&h1, &d1], [&d1, &h1]] {
            let mut rec = Recorder {
                seen: vec![],
                contexts: vec![BarTimeframe::H1, BarTimeframe::D1],
            };
            run_signals(&mut rec, &m15, &contexts).unwrap();
            assert_eq!(
                rec.seen,
                vec![
                    (BarTimeframe::D1, t0),
                    (BarTimeframe::H1, at(23, 0)),
                    (BarTimeframe::M15, at(23, 45)),
                ]
            );
        }
    }

    #[test]
    fn run_signals_checks_the_series_it_is_given() {
        let t0 = monday();
        let flat = [(2.0, 1.0, 1.5); 2];
        let m15 = series(BarTimeframe::M15, t0, &flat);
        let h1 = series(BarTimeframe::H1, t0, &flat);
        let mut rec = Recorder {
            seen: vec![],
            contexts: vec![BarTimeframe::H1],
        };
        assert!(matches!(
            run_signals(&mut rec, &h1, &[]),
            Err(RunError::PrimaryTimeframe { .. })
        ));
        assert!(matches!(
            run_signals(&mut rec, &m15, &[]),
            Err(RunError::Context {
                reason: "missing",
                ..
            })
        ));
        assert!(matches!(
            run_signals(&mut rec, &m15, &[&h1, &h1]),
            Err(RunError::Context {
                reason: "given more than once",
                ..
            })
        ));
        assert!(matches!(
            run_signals(&mut rec, &m15, &[&m15]),
            Err(RunError::Context {
                reason: "not declared by the strategy",
                ..
            })
        ));
    }

    #[test]
    fn exit_reasons_use_the_python_labels() {
        assert_eq!(
            SignalExitReason::FridayCloseCatchup.as_str(),
            "friday_close_catchup"
        );
        assert_eq!(SignalExitReason::ZCrossZero.to_string(), "z_cross_zero");
        assert_eq!(Direction::Short.sign(), -1.0);
    }
}
