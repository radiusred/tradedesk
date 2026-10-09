//! ATR-normalised sizing (`tradedesk/portfolio/risk.py::atr_normalised_size`).
//!
//! Strategies do not size positions for the engine: [`crate::SizingPolicy`] does, and its
//! `AtrRisk` policy uses [`atr_normalised_size`]. A strategy may still need a size of its
//! own, for instance to cap the notional risk at entry, which decides whether an entry
//! fires at all; [`SizingConfig`] holds the inputs such a strategy reads.
//!
//! **Units.** Strategies read raw cache prices (XAUUSD in cents), while `point_value`
//! assumes quote-unit prices. The size therefore works on the ATR in quote units,
//! `atr × raw_scale`, where `raw_scale` is the instrument's (the cost config's
//! `[instruments.X].raw_scale`). A sweep sets it per instrument when the strategy's
//! factory names the config path (`StrategyFactory::raw_scale_path`); the default of 1
//! treats raw prices as quote prices.

use super::{ConfigError, non_negative, positive};

/// The sizing inputs a Python strategy reads from `BaseStrategyConfig`.
#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SizingConfig {
    /// Money risked per trade (`risk_per_trade`). In the Python portfolio this is
    /// reassigned by the risk policy at run time; here it is the configured value.
    pub risk_per_trade: f64,
    /// ATR multiple of the sizing stop (`atr_risk_mult`).
    pub atr_risk_mult: f64,
    /// Minimum size (`min_size`).
    pub min_size: f64,
    /// Maximum size (`max_size`).
    pub max_size: f64,
    /// Account-currency P&L per unit of size for a 1.0 move **in quote units**
    /// (`point_value`).
    pub point_value: f64,
    /// Quote units per raw price unit of the instrument traded (its `raw_scale`: 0.01
    /// for XAUUSD in cents). The size and the cap use `atr × raw_scale`. Defaults to 1
    /// (raw prices are quote prices), which is how the Python code runs on this cache.
    #[serde(default = "one")]
    pub raw_scale: f64,
}

fn one() -> f64 {
    1.0
}

impl Default for SizingConfig {
    /// The `BaseStrategyConfig` defaults.
    fn default() -> Self {
        Self {
            risk_per_trade: 10.0,
            atr_risk_mult: 1.0,
            min_size: 0.1,
            max_size: 5.0,
            point_value: 1.0,
            raw_scale: 1.0,
        }
    }
}

impl SizingConfig {
    /// Check the inputs: the money and multiples finite and non-negative, the size bounds
    /// and scales positive, and `min_size <= max_size`. A strategy whose config embeds a
    /// `SizingConfig` calls this when it is built.
    ///
    /// # Errors
    /// [`ConfigError::Invalid`] naming the first field that fails.
    pub fn validate(&self) -> Result<(), ConfigError> {
        non_negative("risk_per_trade", self.risk_per_trade)?;
        non_negative("atr_risk_mult", self.atr_risk_mult)?;
        non_negative("min_size", self.min_size)?;
        positive("max_size", self.max_size)?;
        positive("point_value", self.point_value)?;
        positive("raw_scale", self.raw_scale)?;
        if self.min_size > self.max_size {
            return Err(ConfigError::Invalid {
                field: "min_size",
                reason: "must not exceed max_size",
            });
        }
        Ok(())
    }

    /// `atr` (raw price units) in quote units: `atr × raw_scale`.
    #[must_use]
    pub fn quote_atr(&self, atr: f64) -> f64 {
        atr * self.raw_scale
    }

    /// [`atr_normalised_size`] with this config's parameters, for an ATR in **raw**
    /// price units (converted with [`SizingConfig::quote_atr`]).
    #[must_use]
    pub fn size(&self, atr: f64, atr_risk_mult: f64) -> f64 {
        atr_normalised_size(
            self.risk_per_trade,
            self.quote_atr(atr),
            atr_risk_mult,
            self.min_size,
            self.max_size,
            self.point_value,
        )
    }
}

/// `risk / (atr * atr_risk_mult * point_value)` clamped to `[min_size, max_size]`;
/// `min_size` when the denominator is not positive.
#[must_use]
pub fn atr_normalised_size(
    risk_per_trade: f64,
    atr: f64,
    atr_risk_mult: f64,
    min_size: f64,
    max_size: f64,
    point_value: f64,
) -> f64 {
    let denom = atr * atr_risk_mult * point_value;
    if denom <= 0.0 {
        return min_size;
    }
    let raw = risk_per_trade / denom;
    min_size.max(max_size.min(raw))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamps_to_the_size_bounds() {
        assert_eq!(atr_normalised_size(10.0, 2.0, 1.0, 0.1, 5.0, 1.0), 5.0);
        assert_eq!(atr_normalised_size(1.0, 4.0, 1.0, 0.1, 5.0, 1.0), 0.25);
        assert_eq!(atr_normalised_size(1.0, 400.0, 1.0, 0.1, 5.0, 1.0), 0.1);
        assert_eq!(atr_normalised_size(1.0, 0.0, 1.0, 0.1, 5.0, 1.0), 0.1);
    }

    #[test]
    fn the_size_works_on_the_quote_unit_atr() {
        let dollars = SizingConfig {
            risk_per_trade: 1.0,
            ..SizingConfig::default()
        };
        let cents = SizingConfig {
            raw_scale: 0.01,
            ..dollars
        };
        // A $4 ATR is 400 raw cents: the same size once the scale is known.
        assert_eq!(dollars.size(4.0, 1.0), 0.25);
        assert_eq!(cents.size(400.0, 1.0), 0.25);
        // Read as dollars, the cents ATR clamps to the minimum size.
        assert_eq!(dollars.size(400.0, 1.0), 0.1);
        assert!(
            SizingConfig {
                raw_scale: 0.0,
                ..SizingConfig::default()
            }
            .validate()
            .is_err()
        );
    }

    #[test]
    fn rejects_inverted_bounds() {
        let bad = SizingConfig {
            min_size: 6.0,
            ..SizingConfig::default()
        };
        assert!(bad.validate().is_err());
        assert!(SizingConfig::default().validate().is_ok());
    }
}
