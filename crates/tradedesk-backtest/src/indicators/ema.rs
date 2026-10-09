//! Exponential moving average of closes (`tradedesk/marketdata/indicators/ema.py`).

use super::{Indicator, IndicatorError, PriceField, check_price};

/// Exponential moving average with `alpha = 2 / (period + 1)`.
///
/// Python parity: the EMA is **seeded with the first close** (not with an SMA) and
/// updated as `(close - ema) * alpha + ema` from the second close on; it only
/// *reports* a value once `period` closes have been seen, though the state evolves
/// from the first.
#[derive(Debug, Clone)]
pub struct Ema {
    period: usize,
    alpha: f64,
    ema: Option<f64>,
    count: usize,
}

impl Ema {
    /// An EMA over `period` closes.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if `period` is 0.
    pub fn new(period: usize) -> Result<Self, IndicatorError> {
        let (period, period_f) = super::period("period", period)?;
        Ok(Self {
            period,
            alpha: 2.0 / (period_f + 1.0),
            ema: None,
            count: 0,
        })
    }

    /// The EMA period.
    #[must_use]
    pub fn period(&self) -> usize {
        self.period
    }
}

impl Indicator for Ema {
    type Input = f64;
    type Output = Option<f64>;

    fn update(&mut self, close: f64) -> Result<Option<f64>, IndicatorError> {
        let close = check_price(PriceField::Close, close)?;
        self.count += 1;
        let next = match self.ema {
            None => close,
            Some(prev) => (close - prev) * self.alpha + prev,
        };
        self.ema = Some(next);
        Ok(self.is_ready().then_some(next))
    }

    fn is_ready(&self) -> bool {
        self.count >= self.period
    }

    fn reset(&mut self) {
        self.ema = None;
        self.count = 0;
    }

    fn warmup_periods(&self) -> usize {
        self.period
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn seeds_with_the_first_close_and_reports_after_period() {
        let mut ema = Ema::new(3).unwrap();
        // alpha = 0.5: 10 -> 10, then (20-10)*0.5+10 = 15, then (30-15)*0.5+15 = 22.5.
        assert_eq!(
            ema.batch(&[10.0, 20.0, 30.0]).unwrap(),
            vec![None, None, Some(22.5)]
        );
        ema.reset();
        assert_eq!(ema.update(7.0).unwrap(), None);
    }
}
