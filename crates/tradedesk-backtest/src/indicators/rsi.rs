//! Relative strength index, Wilder smoothing (`tradedesk/marketdata/indicators/rsi.py`).

use super::{Indicator, IndicatorError, PriceField, check_price};

/// Wilder's RSI.
///
/// Python parity: the first close only records; average gain and loss are seeded from
/// the sums of the first `period` deltas (first value on close `period + 1`) and then
/// smoothed as `(prev * (period - 1) + x) / period`. RSI is `100` when the average
/// loss is 0, otherwise `0` when the average gain is 0, otherwise
/// `100 - 100 / (1 + gain / loss)`.
#[derive(Debug, Clone)]
pub struct Rsi {
    period: usize,
    period_f: f64,
    prev_close: Option<f64>,
    seed_gain: f64,
    seed_loss: f64,
    deltas: usize,
    averages: Option<(f64, f64)>,
}

impl Rsi {
    /// An RSI over `period` deltas.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if `period` is 0.
    pub fn new(period: usize) -> Result<Self, IndicatorError> {
        let (period, period_f) = super::period("period", period)?;
        Ok(Self {
            period,
            period_f,
            prev_close: None,
            seed_gain: 0.0,
            seed_loss: 0.0,
            deltas: 0,
            averages: None,
        })
    }

    fn rsi(avg_gain: f64, avg_loss: f64) -> f64 {
        if avg_loss == 0.0 {
            100.0
        } else if avg_gain == 0.0 {
            0.0
        } else {
            let rs = avg_gain / avg_loss;
            100.0 - (100.0 / (1.0 + rs))
        }
    }
}

impl Indicator for Rsi {
    type Input = f64;
    type Output = Option<f64>;

    fn update(&mut self, close: f64) -> Result<Option<f64>, IndicatorError> {
        let close = check_price(PriceField::Close, close)?;
        let Some(prev) = self.prev_close.replace(close) else {
            return Ok(None);
        };
        let delta = close - prev;
        let gain = delta.max(0.0);
        let loss = (-delta).max(0.0);
        self.deltas += 1;

        let (avg_gain, avg_loss) = match self.averages {
            None => {
                self.seed_gain += gain;
                self.seed_loss += loss;
                if self.deltas < self.period {
                    return Ok(None);
                }
                (
                    self.seed_gain / self.period_f,
                    self.seed_loss / self.period_f,
                )
            }
            Some((g, l)) => (
                (g * (self.period_f - 1.0) + gain) / self.period_f,
                (l * (self.period_f - 1.0) + loss) / self.period_f,
            ),
        };
        self.averages = Some((avg_gain, avg_loss));
        Ok(Some(Self::rsi(avg_gain, avg_loss)))
    }

    fn is_ready(&self) -> bool {
        self.averages.is_some()
    }

    fn reset(&mut self) {
        self.prev_close = None;
        self.seed_gain = 0.0;
        self.seed_loss = 0.0;
        self.deltas = 0;
        self.averages = None;
    }

    fn warmup_periods(&self) -> usize {
        self.period + 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn edge_branches_and_warm_up() {
        let mut up = Rsi::new(2).unwrap();
        assert_eq!(up.warmup_periods(), 3);
        assert_eq!(
            up.batch(&[1.0, 2.0, 3.0]).unwrap(),
            vec![None, None, Some(100.0)]
        );
        let mut down = Rsi::new(2).unwrap();
        assert_eq!(down.batch(&[3.0, 2.0, 1.0]).unwrap()[2], Some(0.0));
        let mut flat = Rsi::new(2).unwrap();
        // No losses at all (flat): avg_loss == 0 wins, RSI = 100.
        assert_eq!(flat.batch(&[2.0, 2.0, 2.0]).unwrap()[2], Some(100.0));
    }

    #[test]
    fn balanced_moves_give_fifty() {
        let mut rsi = Rsi::new(2).unwrap();
        assert_eq!(rsi.batch(&[10.0, 11.0, 10.0]).unwrap()[2], Some(50.0));
    }
}
