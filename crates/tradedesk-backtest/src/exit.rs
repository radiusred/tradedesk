//! Stop and target evaluation (M1-R4): an explicit per-run option, recorded in the
//! ledger metadata.
//!
//! Levels are raw prices on the venue's configured price side (the same side the fill
//! model marks from), and evaluation reads that side's OHLC. A triggered exit is filled
//! by [`crate::FillModel::quote`] at the returned [`PricePoint`], so the venue spread and
//! slippage still apply on top of the level.
//!
//! [`ExitEvaluation::Intrabar`] reads the bar's open, high and low:
//! 1. **Gap.** If the bar opens through a level (at or beyond it), that level is the
//!    first price traded, and the exit fills at the open. A stop gapped through fills
//!    worse than the stop; a target gapped through fills better than the target.
//! 2. Otherwise a long's stop is hit when the low reaches it and its target when the high
//!    reaches it (mirrored for a short), and the exit fills at the level.
//! 3. **Both hit in one bar** (the open lies between them and the range covers both):
//!    the bar's path is unknown, so [`BothHit`] decides. See its variants.
//!
//! [`ExitEvaluation::CloseOnly`] reads the close alone: a level is hit when the close is
//! at or beyond it, and the exit fills at the close. Intrabar excursions are ignored.

use serde::{Deserialize, Serialize};

use crate::Ohlcv;
use crate::cost::fill::PricePoint;
use crate::cost::financing::Direction;

/// How a bar where both the stop and the target are hit is resolved under
/// [`ExitEvaluation::Intrabar`].
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BothHit {
    /// The stop is taken, filled at the stop level. This is the usual "assume the worse
    /// order" rule, and it is the archive's NR7 rule.
    StopFirst,
    /// The stop is taken, filled at the bar's adverse extreme (the low for a long, the
    /// high for a short): the worst price the bar admits. A stress setting.
    Conservative,
}

/// The per-run stop/target evaluation mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case", tag = "mode")]
pub enum ExitEvaluation {
    /// Stops and targets trigger on the bar's high/low.
    Intrabar {
        /// Resolution when one bar hits both levels.
        both_hit: BothHit,
    },
    /// Stops and targets trigger on the bar's close only.
    CloseOnly,
}

/// Why a position left the book.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ExitReason {
    /// The strategy's exit signal.
    Signal,
    /// The stop level.
    Stop,
    /// The target level.
    Target,
    /// Closed because the run's data ended.
    EndOfRun,
}

/// Why a strategy exits: the Python `exit_reason` labels ([`SignalExitReason::as_str`],
/// also its serialised form). The ledger books every strategy exit as
/// [`ExitReason::Signal`] (the `From` impl) and keeps this label beside it
/// (`ClosedTrade::signal_reason`). The vocabulary is closed: a strategy that needs a new
/// label adds a variant, an additive change to the serialised form.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum SignalExitReason {
    /// Close-based loss beyond the ATR stop.
    #[serde(rename = "stop_loss")]
    StopLoss,
    /// Close-based gain beyond the ATR target.
    #[serde(rename = "take_profit")]
    TakeProfit,
    /// The armed breakeven stop was breached on the close.
    #[serde(rename = "breakeven_stop")]
    BreakevenStop,
    /// Close crossed back through a channel's mid-band.
    #[serde(rename = "midband_cross")]
    MidbandCross,
    /// A standardised deviation crossed back through zero.
    #[serde(rename = "z_cross_zero")]
    ZCrossZero,
    /// Held for the maximum number of bars.
    #[serde(rename = "max_hold")]
    MaxHold,
    /// Friday flatten window before the weekend.
    #[serde(rename = "friday_close")]
    FridayClose,
    /// Flattened on a bar after the Friday deadline (late Friday or the weekend).
    #[serde(rename = "friday_close_catchup")]
    FridayCloseCatchup,
    /// Close back through an EMA and the MACD histogram flipped.
    #[serde(rename = "ema_cross+macd_flip")]
    EmaCrossMacdFlip,
    /// Close back through an EMA.
    #[serde(rename = "ema_cross")]
    EmaCross,
    /// The MACD histogram flipped.
    #[serde(rename = "macd_flip")]
    MacdFlip,
    /// A regime filter switched off.
    #[serde(rename = "regime_off")]
    RegimeOff,
    /// The close reached the middle of a band.
    #[serde(rename = "take_profit_bb_middle")]
    TakeProfitBbMiddle,
    /// Held for a time-stop number of bars.
    #[serde(rename = "time_stop")]
    TimeStop,
    /// The close gave back too much of the maximum favourable excursion.
    #[serde(rename = "giveback")]
    Giveback,
    /// The close fell through a wide (catastrophic) ATR stop.
    #[serde(rename = "catastrophic_stop")]
    CatastrophicStop,
    /// The close rose above an exit SMA. The label is `sma5_exit` whatever the SMA's
    /// period.
    #[serde(rename = "sma5_exit")]
    Sma5Exit,
    /// RSI rose above an exit threshold.
    #[serde(rename = "rsi_exit")]
    RsiExit,
    /// A calendar exit day was reached.
    #[serde(rename = "calendar_exit")]
    CalendarExit,
}

impl SignalExitReason {
    /// The Python label, e.g. `"stop_loss"`.
    #[must_use]
    pub fn as_str(self) -> &'static str {
        match self {
            Self::StopLoss => "stop_loss",
            Self::TakeProfit => "take_profit",
            Self::BreakevenStop => "breakeven_stop",
            Self::MidbandCross => "midband_cross",
            Self::ZCrossZero => "z_cross_zero",
            Self::MaxHold => "max_hold",
            Self::FridayClose => "friday_close",
            Self::FridayCloseCatchup => "friday_close_catchup",
            Self::EmaCrossMacdFlip => "ema_cross+macd_flip",
            Self::EmaCross => "ema_cross",
            Self::MacdFlip => "macd_flip",
            Self::RegimeOff => "regime_off",
            Self::TakeProfitBbMiddle => "take_profit_bb_middle",
            Self::TimeStop => "time_stop",
            Self::Giveback => "giveback",
            Self::CatastrophicStop => "catastrophic_stop",
            Self::Sma5Exit => "sma5_exit",
            Self::RsiExit => "rsi_exit",
            Self::CalendarExit => "calendar_exit",
        }
    }
}

impl std::fmt::Display for SignalExitReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Every strategy exit is booked as a signal exit.
impl From<SignalExitReason> for ExitReason {
    fn from(_: SignalExitReason) -> Self {
        Self::Signal
    }
}

/// A triggered stop or target, and where in the bar to fill it.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct ExitTrigger {
    /// [`ExitReason::Stop`] or [`ExitReason::Target`].
    pub reason: ExitReason,
    /// The fill point: the open for a gap, the close under `CloseOnly`, otherwise a level.
    pub at: PricePoint,
}

impl ExitEvaluation {
    /// Evaluate one bar (configured side, raw units) for a position with optional `stop`
    /// and `target` levels. Returns `None` when neither is hit.
    #[must_use]
    pub fn evaluate(
        self,
        bar: &Ohlcv,
        direction: Direction,
        stop: Option<f64>,
        target: Option<f64>,
    ) -> Option<ExitTrigger> {
        // Signed so that "beyond" is always `>=`: for a long, a stop is hit by a price at or
        // below it, which is `-price >= -stop`.
        let s = direction.sign();
        let adverse = |price: f64, level: f64| -s * price >= -s * level;
        let favourable = |price: f64, level: f64| s * price >= s * level;
        let trigger = |reason, at| Some(ExitTrigger { reason, at });
        match self {
            Self::CloseOnly => {
                if stop.is_some_and(|l| adverse(bar.close, l)) {
                    trigger(ExitReason::Stop, PricePoint::Close)
                } else if target.is_some_and(|l| favourable(bar.close, l)) {
                    trigger(ExitReason::Target, PricePoint::Close)
                } else {
                    None
                }
            }
            Self::Intrabar { both_hit } => {
                if stop.is_some_and(|l| adverse(bar.open, l)) {
                    return trigger(ExitReason::Stop, PricePoint::Open);
                }
                if target.is_some_and(|l| favourable(bar.open, l)) {
                    return trigger(ExitReason::Target, PricePoint::Open);
                }
                let (worst, best) = match direction {
                    Direction::Long => (bar.low, bar.high),
                    Direction::Short => (bar.high, bar.low),
                };
                let stop_hit = stop.filter(|&l| adverse(worst, l));
                let target_hit = target.filter(|&l| favourable(best, l));
                match (stop_hit, target_hit) {
                    (Some(level), Some(_)) => match both_hit {
                        BothHit::StopFirst => trigger(ExitReason::Stop, PricePoint::Level(level)),
                        BothHit::Conservative => {
                            trigger(ExitReason::Stop, PricePoint::Level(worst))
                        }
                    },
                    (Some(level), None) => trigger(ExitReason::Stop, PricePoint::Level(level)),
                    (None, Some(level)) => trigger(ExitReason::Target, PricePoint::Level(level)),
                    (None, None) => None,
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const STOP_FIRST: ExitEvaluation = ExitEvaluation::Intrabar {
        both_hit: BothHit::StopFirst,
    };
    const CONSERVATIVE: ExitEvaluation = ExitEvaluation::Intrabar {
        both_hit: BothHit::Conservative,
    };

    fn ohlc(open: f64, high: f64, low: f64, close: f64) -> Ohlcv {
        Ohlcv {
            open,
            high,
            low,
            close,
            tick_volume: 1.0,
        }
    }

    #[allow(clippy::unnecessary_wraps)]
    fn hit(reason: ExitReason, at: PricePoint) -> Option<ExitTrigger> {
        Some(ExitTrigger { reason, at })
    }

    #[test]
    fn intrabar_long_stop_and_target_fill_at_their_levels() {
        let (stop, target) = (Some(95.0), Some(110.0));
        let b = ohlc(100.0, 104.0, 94.0, 101.0);
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Long, stop, target),
            hit(ExitReason::Stop, PricePoint::Level(95.0))
        );
        let b = ohlc(100.0, 111.0, 99.0, 109.0);
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Long, stop, target),
            hit(ExitReason::Target, PricePoint::Level(110.0))
        );
        let b = ohlc(100.0, 109.0, 96.0, 100.0);
        assert_eq!(STOP_FIRST.evaluate(&b, Direction::Long, stop, target), None);
    }

    #[test]
    fn intrabar_short_is_mirrored() {
        let (stop, target) = (Some(105.0), Some(90.0));
        let b = ohlc(100.0, 106.0, 97.0, 99.0);
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Short, stop, target),
            hit(ExitReason::Stop, PricePoint::Level(105.0))
        );
        let b = ohlc(100.0, 101.0, 89.0, 92.0);
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Short, stop, target),
            hit(ExitReason::Target, PricePoint::Level(90.0))
        );
    }

    #[test]
    fn touching_a_level_exactly_counts_as_a_hit() {
        let b = ohlc(100.0, 110.0, 95.0, 100.0);
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Long, Some(95.0), None),
            hit(ExitReason::Stop, PricePoint::Level(95.0))
        );
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Long, None, Some(110.0)),
            hit(ExitReason::Target, PricePoint::Level(110.0))
        );
    }

    #[test]
    fn both_hit_in_one_bar_resolves_by_the_sub_option() {
        let b = ohlc(100.0, 112.0, 93.0, 104.0);
        let (stop, target) = (Some(95.0), Some(110.0));
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Long, stop, target),
            hit(ExitReason::Stop, PricePoint::Level(95.0))
        );
        assert_eq!(
            CONSERVATIVE.evaluate(&b, Direction::Long, stop, target),
            hit(ExitReason::Stop, PricePoint::Level(93.0))
        );
        let (stop, target) = (Some(110.0), Some(95.0));
        assert_eq!(
            STOP_FIRST.evaluate(&b, Direction::Short, stop, target),
            hit(ExitReason::Stop, PricePoint::Level(110.0))
        );
        assert_eq!(
            CONSERVATIVE.evaluate(&b, Direction::Short, stop, target),
            hit(ExitReason::Stop, PricePoint::Level(112.0))
        );
    }

    #[test]
    fn a_gap_through_a_level_fills_at_the_open_before_any_both_hit_rule() {
        let (stop, target) = (Some(95.0), Some(110.0));
        // Opens below the stop, then trades up through the target: the stop was first.
        let b = ohlc(92.0, 111.0, 91.0, 105.0);
        for mode in [STOP_FIRST, CONSERVATIVE] {
            assert_eq!(
                mode.evaluate(&b, Direction::Long, stop, target),
                hit(ExitReason::Stop, PricePoint::Open)
            );
        }
        // Opens above the target, then trades down through the stop: the target was first.
        let b = ohlc(113.0, 114.0, 94.0, 96.0);
        for mode in [STOP_FIRST, CONSERVATIVE] {
            assert_eq!(
                mode.evaluate(&b, Direction::Long, stop, target),
                hit(ExitReason::Target, PricePoint::Open)
            );
        }
    }

    #[test]
    fn close_only_ignores_the_range_and_fills_at_the_close() {
        let (stop, target) = (Some(95.0), Some(110.0));
        let wick = ohlc(100.0, 112.0, 93.0, 104.0);
        assert_eq!(
            ExitEvaluation::CloseOnly.evaluate(&wick, Direction::Long, stop, target),
            None
        );
        let below = ohlc(100.0, 101.0, 93.0, 94.0);
        assert_eq!(
            ExitEvaluation::CloseOnly.evaluate(&below, Direction::Long, stop, target),
            hit(ExitReason::Stop, PricePoint::Close)
        );
        let above = ohlc(100.0, 112.0, 99.0, 111.0);
        assert_eq!(
            ExitEvaluation::CloseOnly.evaluate(&above, Direction::Long, stop, target),
            hit(ExitReason::Target, PricePoint::Close)
        );
        assert_eq!(
            ExitEvaluation::CloseOnly.evaluate(&below, Direction::Short, Some(105.0), Some(94.0)),
            hit(ExitReason::Target, PricePoint::Close)
        );
    }

    #[test]
    fn signal_exit_reasons_serialise_as_their_python_labels_and_book_as_signal() {
        use SignalExitReason as S;
        let all = [
            S::StopLoss,
            S::TakeProfit,
            S::BreakevenStop,
            S::MidbandCross,
            S::ZCrossZero,
            S::MaxHold,
            S::FridayClose,
            S::FridayCloseCatchup,
            S::EmaCrossMacdFlip,
            S::EmaCross,
            S::MacdFlip,
            S::RegimeOff,
            S::TakeProfitBbMiddle,
            S::TimeStop,
            S::Giveback,
            S::CatastrophicStop,
            S::Sma5Exit,
            S::RsiExit,
            S::CalendarExit,
        ];
        for reason in all {
            let json = serde_json::to_string(&reason).unwrap();
            assert_eq!(json, format!("\"{}\"", reason.as_str()));
            assert_eq!(serde_json::from_str::<S>(&json).unwrap(), reason);
            assert_eq!(ExitReason::from(reason), ExitReason::Signal);
        }
        // The booked reasons keep their labels.
        assert_eq!(
            serde_json::to_string(&ExitReason::EndOfRun).unwrap(),
            "\"end_of_run\""
        );
        // One Direction: the strategy's is the ledger's.
        let d: crate::strategy::Direction = Direction::Short;
        assert_eq!(d, crate::Direction::Short);
    }

    #[test]
    fn no_levels_never_trigger() {
        let b = ohlc(100.0, 200.0, 1.0, 100.0);
        for mode in [STOP_FIRST, CONSERVATIVE, ExitEvaluation::CloseOnly] {
            assert_eq!(mode.evaluate(&b, Direction::Long, None, None), None);
        }
    }
}
