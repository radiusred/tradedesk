//! Average true range, Wilder smoothing (`tradedesk/marketdata/indicators/atr.py`).

use std::collections::VecDeque;

use super::{Indicator, IndicatorError, check_bar, sum};
use crate::Ohlcv;

/// Wilder's average true range.
///
/// Python parity: the first bar's true range is `high - low` (there is no previous
/// close); later bars use `max(high - low, |high - prev_close|, |low - prev_close|)`.
/// The ATR is seeded with the SMA of the first `period` true ranges and then smoothed
/// as `(prev * (period - 1) + tr) / period`. First value on the `period`-th bar.
#[derive(Debug, Clone)]
pub struct Atr {
    period: usize,
    period_f: f64,
    trs: VecDeque<f64>,
    prev_close: Option<f64>,
    atr: Option<f64>,
}

impl Atr {
    /// An ATR over `period` bars.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if `period` is 0.
    pub fn new(period: usize) -> Result<Self, IndicatorError> {
        let (period, period_f) = super::period("period", period)?;
        Ok(Self {
            period,
            period_f,
            trs: VecDeque::with_capacity(period),
            prev_close: None,
            atr: None,
        })
    }

    /// The latest ATR, if ready.
    #[must_use]
    pub fn value(&self) -> Option<f64> {
        self.atr
    }
}

/// True range of `bar` against the previous close (Python's `max` of three).
pub(crate) fn true_range(bar: &Ohlcv, prev_close: Option<f64>) -> f64 {
    let range = bar.high - bar.low;
    match prev_close {
        None => range,
        Some(prev) => range
            .max((bar.high - prev).abs())
            .max((bar.low - prev).abs()),
    }
}

impl Indicator for Atr {
    type Input = Ohlcv;
    type Output = Option<f64>;

    fn update(&mut self, bar: Ohlcv) -> Result<Option<f64>, IndicatorError> {
        check_bar(&bar)?;
        let tr = true_range(&bar, self.prev_close);
        if self.trs.len() == self.period {
            self.trs.pop_front();
        }
        self.trs.push_back(tr);
        self.prev_close = Some(bar.close);

        if !self.is_ready() {
            return Ok(None);
        }
        let next = match self.atr {
            None => sum(self.trs.iter().copied()) / self.period_f,
            Some(prev) => (prev * (self.period_f - 1.0) + tr) / self.period_f,
        };
        self.atr = Some(next);
        Ok(Some(next))
    }

    fn is_ready(&self) -> bool {
        self.trs.len() >= self.period
    }

    fn reset(&mut self) {
        self.trs.clear();
        self.prev_close = None;
        self.atr = None;
    }

    fn warmup_periods(&self) -> usize {
        self.period
    }
}

#[cfg(test)]
mod tests {
    use super::super::test_support::hlc;
    use super::*;

    #[test]
    fn seeds_with_the_mean_true_range_then_smooths() {
        let mut atr = Atr::new(2).unwrap();
        // TR1 = 2 (no prev close); TR2 = max(1, |12-10|, |11-10|) = 2 -> seed 2;
        // TR3 = max(1, |13-11.5|, |12-11.5|) = 1.5 -> (2*1 + 1.5)/2 = 1.75.
        let out = atr
            .batch(&[
                hlc(11.0, 9.0, 10.0),
                hlc(12.0, 11.0, 11.5),
                hlc(13.0, 12.0, 12.5),
            ])
            .unwrap();
        assert_eq!(out, vec![None, Some(2.0), Some(1.75)]);
        assert_eq!(atr.value(), Some(1.75));
        assert_eq!(atr.warmup_periods(), 2);
    }

    #[test]
    fn rejects_a_corrupt_bar_without_touching_state() {
        let mut atr = Atr::new(1).unwrap();
        assert_eq!(atr.update(hlc(11.0, 9.0, 10.0)).unwrap(), Some(2.0));
        assert!(atr.update(hlc(9.0, 11.0, 10.0)).is_err());
        // prev_close is still 10: TR = max(1, |12-10|, |11-10|) = 2.
        assert_eq!(atr.update(hlc(12.0, 11.0, 11.5)).unwrap(), Some(2.0));
    }
}
