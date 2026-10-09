//! MACD (`tradedesk/marketdata/indicators/macd.py`).

use std::collections::VecDeque;

use super::{Indicator, IndicatorError, PriceField, check_price, sum};

/// One [`Macd`] value.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MacdOutput {
    /// Fast EMA minus slow EMA.
    pub macd: f64,
    /// EMA of the MACD line.
    pub signal: f64,
    /// `macd - signal`.
    pub histogram: f64,
}

/// MACD: fast EMA − slow EMA, a signal EMA of that line, and their difference.
///
/// Python parity, quirks included:
/// - each EMA uses `k = 2 / (n + 1)` and the form `close * k + ema * (1 - k)`;
/// - the fast and slow EMAs are seeded with the SMA of their first `fast` / `slow`
///   closes, **and the seed bar is then updated again with that same close** (the
///   reference seeds and updates in the same call);
/// - the signal line is seeded the same way from the first `signal` MACD values;
/// - nothing is reported until the signal line exists: the first value comes on bar
///   `slow + signal - 1`.
#[derive(Debug, Clone)]
pub struct Macd {
    fast: usize,
    slow: usize,
    signal: usize,
    fast_f: f64,
    slow_f: f64,
    signal_f: f64,
    fast_k: f64,
    slow_k: f64,
    signal_k: f64,
    closes: VecDeque<f64>,
    macd_values: VecDeque<f64>,
    fast_ema: Option<f64>,
    slow_ema: Option<f64>,
    signal_ema: Option<f64>,
}

impl Macd {
    /// MACD with the given fast, slow and signal periods (12, 26, 9 is standard).
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if any period is 0.
    pub fn new(fast: usize, slow: usize, signal: usize) -> Result<Self, IndicatorError> {
        let (fast, fast_f) = super::period("fast", fast)?;
        let (slow, slow_f) = super::period("slow", slow)?;
        let (signal, signal_f) = super::period("signal", signal)?;
        Ok(Self {
            fast,
            slow,
            signal,
            fast_f,
            slow_f,
            signal_f,
            fast_k: 2.0 / (fast_f + 1.0),
            slow_k: 2.0 / (slow_f + 1.0),
            signal_k: 2.0 / (signal_f + 1.0),
            closes: VecDeque::with_capacity(slow),
            macd_values: VecDeque::with_capacity(signal),
            fast_ema: None,
            slow_ema: None,
            signal_ema: None,
        })
    }
}

fn ema_step(prev: f64, x: f64, k: f64) -> f64 {
    (x * k) + (prev * (1.0 - k))
}

impl Indicator for Macd {
    type Input = f64;
    type Output = Option<MacdOutput>;

    fn update(&mut self, close: f64) -> Result<Option<MacdOutput>, IndicatorError> {
        let close = check_price(PriceField::Close, close)?;
        if self.closes.len() == self.slow {
            self.closes.pop_front();
        }
        self.closes.push_back(close);

        if self.fast_ema.is_none() && self.closes.len() >= self.fast {
            let skip = self.closes.len() - self.fast;
            self.fast_ema = Some(sum(self.closes.iter().skip(skip).copied()) / self.fast_f);
        }
        if self.slow_ema.is_none() && self.closes.len() >= self.slow {
            self.slow_ema = Some(sum(self.closes.iter().copied()) / self.slow_f);
        }
        self.fast_ema = self.fast_ema.map(|e| ema_step(e, close, self.fast_k));
        self.slow_ema = self.slow_ema.map(|e| ema_step(e, close, self.slow_k));

        let (Some(fast), Some(slow)) = (self.fast_ema, self.slow_ema) else {
            return Ok(None);
        };
        let macd = fast - slow;
        if self.macd_values.len() == self.signal {
            self.macd_values.pop_front();
        }
        self.macd_values.push_back(macd);

        if self.signal_ema.is_none() && self.macd_values.len() >= self.signal {
            self.signal_ema = Some(sum(self.macd_values.iter().copied()) / self.signal_f);
        }
        self.signal_ema = self.signal_ema.map(|e| ema_step(e, macd, self.signal_k));

        Ok(self.signal_ema.map(|signal| MacdOutput {
            macd,
            signal,
            histogram: macd - signal,
        }))
    }

    fn is_ready(&self) -> bool {
        self.fast_ema.is_some() && self.slow_ema.is_some() && self.signal_ema.is_some()
    }

    fn reset(&mut self) {
        self.closes.clear();
        self.macd_values.clear();
        self.fast_ema = None;
        self.slow_ema = None;
        self.signal_ema = None;
    }

    fn warmup_periods(&self) -> usize {
        self.slow + self.signal - 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_value_on_bar_slow_plus_signal_minus_one() {
        let mut macd = Macd::new(2, 3, 2).unwrap();
        assert_eq!(macd.warmup_periods(), 4);
        let out = macd.batch(&[1.0, 2.0, 3.0, 4.0, 5.0]).unwrap();
        assert!(out[..3].iter().all(Option::is_none));
        assert!(out[3].is_some() && out[4].is_some());
        assert!(macd.is_ready());
    }

    #[test]
    fn seed_bar_is_updated_twice_like_the_reference() {
        // fast = slow = signal = 1: k = 1, so every EMA is just the latest input,
        // regardless of the seed; check instead with fast = 2 on a two-close input.
        let mut macd = Macd::new(2, 2, 1).unwrap();
        let out = macd.batch(&[10.0, 20.0]).unwrap();
        // Seed = (10 + 20) / 2 = 15, then updated with 20 at k = 2/3:
        // 20 * 2/3 + 15 * 1/3 = 18.333...; fast == slow so MACD = 0.
        assert_eq!(out[1].unwrap().macd, 0.0);
        let k = 2.0 / 3.0;
        assert_eq!(macd.fast_ema, Some(20.0 * k + 15.0 * (1.0 - k)));
    }
}
