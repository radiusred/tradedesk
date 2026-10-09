//! The one test strategy: [`ThresholdCross`], registered for sweeps as
//! [`NAME`] by [`registry`].
//!
//! It goes long on the bar whose bid close crosses up through a fixed level (the previous
//! close at or below it, this close above it), with a stop and a target at fixed raw-unit
//! distances from that close, and exits (`stop_loss`) on a close below a second level.
//! After an exit, an external close or a rejected entry it waits `cooldown_bars` bars
//! before it may enter again. It exists to drive the engine, the sweep and the command
//! line in tests, and makes no claim to be a trading idea.
//!
//! The integration tests use it as a module (`mod toy;`); the crate's own unit tests
//! include this same file under `cfg(test)` (see `src/lib.rs`).
#![allow(dead_code)]

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tradedesk_backtest::strategy::{
    ConfigError, Entry, PositionFeedback, Signal, SignalKind, Strategy, StrategyError,
    checked_fill, checked_side, non_negative, positive,
};
use tradedesk_backtest::sweep::{BaseConfig, BoxedStrategy, StrategyFactory, StrategyRegistry};
use tradedesk_backtest::{BarTimeframe, Direction, JoinedBar, SignalExitReason};
use tradedesk_data::Side;

/// The name [`registry`] registers the strategy under.
pub const NAME: &str = "threshold_cross";

/// What [`ThresholdCross`] does. Prices are raw instrument units.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ThresholdConfig {
    /// The primary timeframe.
    pub timeframe: BarTimeframe,
    /// Enter long on a close above this level after a close at or below it.
    pub enter_above: f64,
    /// Exit on a close below this level.
    pub exit_below: f64,
    /// The stop's distance below the entry close (> 0); also the entry's `atr`.
    pub stop_distance: f64,
    /// The target's distance above the entry close (> 0).
    pub target_distance: f64,
    /// Bars to wait after an exit or a rejected entry before entering again.
    pub cooldown_bars: u32,
    /// The price scale the strategy declares ([`Strategy::price_scale`]), if any.
    pub price_scale: Option<f64>,
}

impl Default for ThresholdConfig {
    /// Daily bars, levels for the synthetic gold path in cents: enter on a cross of
    /// $2030, a $50 stop, a $150 target, exit below $2030, a one-bar cooldown.
    fn default() -> Self {
        Self {
            timeframe: BarTimeframe::D1,
            enter_above: 203_000.0,
            exit_below: 203_000.0,
            stop_distance: 5_000.0,
            target_distance: 15_000.0,
            cooldown_bars: 1,
            price_scale: None,
        }
    }
}

/// The test strategy (see the module docs).
#[derive(Debug, Clone)]
pub struct ThresholdCross {
    config: ThresholdConfig,
    prev_close: Option<f64>,
    long: bool,
    cooldown: u32,
}

impl ThresholdCross {
    /// A flat strategy with no history.
    ///
    /// # Errors
    /// [`ConfigError::Invalid`] for a non-finite or negative level, a distance that is not
    /// positive, or a price scale that is not positive.
    pub fn new(config: ThresholdConfig) -> Result<Self, ConfigError> {
        non_negative("enter_above", config.enter_above)?;
        non_negative("exit_below", config.exit_below)?;
        positive("stop_distance", config.stop_distance)?;
        positive("target_distance", config.target_distance)?;
        if let Some(scale) = config.price_scale {
            positive("price_scale", scale)?;
        }
        Ok(Self {
            config,
            prev_close: None,
            long: false,
            cooldown: 0,
        })
    }

    /// The config it was built with.
    #[must_use]
    pub fn config(&self) -> &ThresholdConfig {
        &self.config
    }

    fn flatten(&mut self) {
        self.long = false;
        self.cooldown = self.config.cooldown_bars;
    }

    fn require_position(&self) -> Result<(), StrategyError> {
        if self.long {
            Ok(())
        } else {
            Err(StrategyError::Feedback {
                reason: "no signalled position",
            })
        }
    }
}

impl Strategy for ThresholdCross {
    fn name(&self) -> &'static str {
        NAME
    }

    fn primary_timeframe(&self) -> BarTimeframe {
        self.config.timeframe
    }

    fn price_scale(&self) -> Option<f64> {
        self.config.price_scale
    }

    fn on_bar(&mut self, bar: &JoinedBar) -> Result<Option<Signal>, StrategyError> {
        let close = checked_side(bar, Side::Bid)?.close;
        let prev = self.prev_close.replace(close);
        self.cooldown = self.cooldown.saturating_sub(1);
        let signal = |kind| {
            Some(Signal {
                ts_open_utc: bar.ts_open_utc,
                kind,
            })
        };
        if self.long {
            if close < self.config.exit_below {
                self.flatten();
                return Ok(signal(SignalKind::Exit {
                    reason: SignalExitReason::StopLoss,
                }));
            }
            return Ok(None);
        }
        let level = self.config.enter_above;
        let crossed = prev.is_some_and(|p| p <= level) && close > level;
        if !crossed || self.cooldown > 0 {
            return Ok(None);
        }
        self.long = true;
        Ok(signal(SignalKind::Enter(Entry {
            direction: Direction::Long,
            reference_price: close,
            stop: Some(close - self.config.stop_distance),
            target: Some(close + self.config.target_distance),
            atr: self.config.stop_distance,
        })))
    }

    fn on_feedback(&mut self, feedback: PositionFeedback) -> Result<(), StrategyError> {
        self.require_position()?;
        match feedback {
            PositionFeedback::EntryFilled { price } => {
                checked_fill(price)?;
            }
            PositionFeedback::EntryRejected | PositionFeedback::ClosedExternally => {
                self.flatten();
            }
        }
        Ok(())
    }

    fn position(&self) -> Option<Direction> {
        self.long.then_some(Direction::Long)
    }
}

/// Builds [`ThresholdCross`] for sweeps. It has a default config only, and takes each
/// instrument's `raw_scale` at `price_scale`.
#[derive(Debug, Clone, Copy)]
pub struct ThresholdFactory;

impl StrategyFactory for ThresholdFactory {
    fn base_config(&self, base: BaseConfig) -> Result<Value, String> {
        match base {
            BaseConfig::Default => {
                Ok(serde_json::to_value(ThresholdConfig::default()).expect("the config is JSON"))
            }
            BaseConfig::Frozen => Err(format!(
                "strategy {NAME:?} has no frozen config; use base = \"default\""
            )),
        }
    }

    fn build(&self, config: Value) -> Result<BoxedStrategy, String> {
        let config: ThresholdConfig = serde_json::from_value(config).map_err(|e| e.to_string())?;
        Ok(Box::new(
            ThresholdCross::new(config).map_err(|e| e.to_string())?,
        ))
    }

    fn raw_scale_path(&self) -> Option<&str> {
        Some("price_scale")
    }
}

/// A registry holding the test strategy under [`NAME`].
#[must_use]
pub fn registry() -> StrategyRegistry {
    let mut strategies = StrategyRegistry::new();
    strategies
        .register(NAME, ThresholdFactory)
        .expect("one registration");
    strategies
}
