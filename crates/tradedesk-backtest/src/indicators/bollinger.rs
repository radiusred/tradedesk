//! Bollinger bands (`tradedesk/marketdata/indicators/bollinger_bands.py`).

use std::collections::VecDeque;

use super::{Indicator, IndicatorError, PriceField, check_price, sum};

/// One [`BollingerBands`] value.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct BollingerOutput {
    /// SMA of the window.
    pub middle: f64,
    /// `middle + k * std`.
    pub upper: f64,
    /// `middle - k * std`.
    pub lower: f64,
    /// Population standard deviation of the window (ddof 0).
    pub std: f64,
}

/// Bollinger bands: SMA ± `k` population standard deviations.
///
/// Python parity: population standard deviation (ddof 0), computed two-pass over the
/// window on every update (`sum((x - mean) ** 2) / period`); first value on the
/// `period`-th close.
#[derive(Debug, Clone)]
pub struct BollingerBands {
    period: usize,
    period_f: f64,
    k: f64,
    closes: VecDeque<f64>,
}

impl BollingerBands {
    /// Bands over `period` closes at `k` standard deviations.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if `period` is 0 or `k` is not a positive
    /// finite number.
    pub fn new(period: usize, k: f64) -> Result<Self, IndicatorError> {
        let (period, period_f) = super::period("period", period)?;
        if !(k.is_finite() && k > 0.0) {
            return Err(IndicatorError::InvalidParameter {
                name: "k",
                reason: "must be finite and > 0",
            });
        }
        Ok(Self {
            period,
            period_f,
            k,
            closes: VecDeque::with_capacity(period),
        })
    }
}

impl Indicator for BollingerBands {
    type Input = f64;
    type Output = Option<BollingerOutput>;

    fn update(&mut self, close: f64) -> Result<Option<BollingerOutput>, IndicatorError> {
        let close = check_price(PriceField::Close, close)?;
        if self.closes.len() == self.period {
            self.closes.pop_front();
        }
        self.closes.push_back(close);
        if !self.is_ready() {
            return Ok(None);
        }
        let mean = sum(self.closes.iter().copied()) / self.period_f;
        let var = sum(self.closes.iter().map(|x| (x - mean) * (x - mean))) / self.period_f;
        let std = var.sqrt();
        Ok(Some(BollingerOutput {
            middle: mean,
            upper: mean + self.k * std,
            lower: mean - self.k * std,
            std,
        }))
    }

    fn is_ready(&self) -> bool {
        self.closes.len() >= self.period
    }

    fn reset(&mut self) {
        self.closes.clear();
    }

    fn warmup_periods(&self) -> usize {
        self.period
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn population_std_bands() {
        let mut bb = BollingerBands::new(4, 2.0).unwrap();
        let out = bb.batch(&[2.0, 4.0, 4.0, 6.0]).unwrap();
        assert_eq!(out[2], None);
        // mean 4, population var = (4 + 0 + 0 + 4) / 4 = 2.
        let v = out[3].unwrap();
        assert_eq!(v.middle, 4.0);
        assert_eq!(v.std, 2.0_f64.sqrt());
        assert_eq!(v.upper, 4.0 + 2.0 * 2.0_f64.sqrt());
        assert_eq!(v.lower, 4.0 - 2.0 * 2.0_f64.sqrt());
    }

    #[test]
    fn rejects_non_positive_k() {
        for k in [0.0, -1.0, f64::NAN] {
            assert!(BollingerBands::new(20, k).is_err());
        }
    }
}
