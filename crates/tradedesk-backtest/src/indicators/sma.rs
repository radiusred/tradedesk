//! Simple moving average of closes (`tradedesk/marketdata/indicators/sma.py`).

use std::collections::VecDeque;

use super::{Indicator, IndicatorError, PriceField, check_price, sum};

/// Simple moving average of the last `period` closes.
///
/// Python parity: the first value is produced on the `period`-th close; the window is
/// re-summed on every update with `CPython`'s compensated `sum(deque) / period`, never
/// kept as a running sum, so values match the reference bit for bit.
#[derive(Debug, Clone)]
pub struct Sma {
    period: usize,
    period_f: f64,
    closes: VecDeque<f64>,
}

impl Sma {
    /// An SMA over `period` closes.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if `period` is 0.
    pub fn new(period: usize) -> Result<Self, IndicatorError> {
        let (period, period_f) = super::period("period", period)?;
        Ok(Self {
            period,
            period_f,
            closes: VecDeque::with_capacity(period),
        })
    }

    /// The window length.
    #[must_use]
    pub fn period(&self) -> usize {
        self.period
    }
}

impl Indicator for Sma {
    type Input = f64;
    type Output = Option<f64>;

    fn update(&mut self, close: f64) -> Result<Option<f64>, IndicatorError> {
        let close = check_price(PriceField::Close, close)?;
        if self.closes.len() == self.period {
            self.closes.pop_front();
        }
        self.closes.push_back(close);
        Ok(self
            .is_ready()
            .then(|| sum(self.closes.iter().copied()) / self.period_f))
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
    fn first_value_on_the_period_th_close() {
        let mut sma = Sma::new(3).unwrap();
        assert_eq!(sma.warmup_periods(), 3);
        assert_eq!(
            sma.batch(&[1.0, 2.0, 3.0, 4.0]).unwrap(),
            vec![None, None, Some(2.0), Some(3.0)]
        );
        assert!(sma.is_ready());
        sma.reset();
        assert!(!sma.is_ready());
        assert_eq!(sma.update(5.0).unwrap(), None);
    }

    #[test]
    fn a_rejected_close_leaves_the_window_unchanged() {
        let mut sma = Sma::new(2).unwrap();
        sma.update(1.0).unwrap();
        assert!(sma.update(-3.0).is_err());
        assert_eq!(sma.update(3.0).unwrap(), Some(2.0));
    }
}
