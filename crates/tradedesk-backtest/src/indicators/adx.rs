//! Average directional index, Wilder smoothing (`tradedesk/marketdata/indicators/adx.py`).

use super::atr::true_range;
use super::{Indicator, IndicatorError, check_bar};
use crate::Ohlcv;

/// One [`Adx`] update. Each field is `None` until its own warm-up completes.
#[derive(Debug, Clone, Copy, PartialEq, Default)]
pub struct AdxOutput {
    /// ADX, from bar `2 * period`.
    pub adx: Option<f64>,
    /// +DI, from bar `period + 1`.
    pub plus_di: Option<f64>,
    /// −DI, from bar `period + 1`.
    pub minus_di: Option<f64>,
}

#[derive(Debug, Clone, Copy)]
struct Prev {
    high: f64,
    low: f64,
    close: f64,
}

/// Wilder's ADX with +DI and −DI.
///
/// Python parity:
/// - the first bar only records high/low/close;
/// - each later bar contributes a true range and directional movement
///   (`+DM = up` when `up > down` and `up > 0`, `−DM = down` when `down > up` and
///   `down > 0`);
/// - TR, +DM and −DM are summed over the first `period` deltas, then smoothed with
///   Wilder's *sum* form `prev - prev / period + x`;
/// - DI is `100 * dm / tr` (both 0 when the smoothed TR is 0), DX is
///   `100 * |+DI − −DI| / (+DI + −DI)` (0 when the sum is 0);
/// - ADX is seeded with the SMA of the first `period` DX values, then smoothed as
///   `(prev * (period - 1) + dx) / period`.
#[derive(Debug, Clone)]
pub struct Adx {
    period: usize,
    period_f: f64,
    prev: Option<Prev>,
    seed_tr: f64,
    seed_pdm: f64,
    seed_mdm: f64,
    deltas: usize,
    smoothed: Option<(f64, f64, f64)>,
    dx_seed: f64,
    dx_count: usize,
    adx: Option<f64>,
}

impl Adx {
    /// An ADX over `period` bars.
    ///
    /// # Errors
    /// [`IndicatorError::InvalidParameter`] if `period` is 0.
    pub fn new(period: usize) -> Result<Self, IndicatorError> {
        let (period, period_f) = super::period("period", period)?;
        Ok(Self {
            period,
            period_f,
            prev: None,
            seed_tr: 0.0,
            seed_pdm: 0.0,
            seed_mdm: 0.0,
            deltas: 0,
            smoothed: None,
            dx_seed: 0.0,
            dx_count: 0,
            adx: None,
        })
    }

    fn di(tr: f64, pdm: f64, mdm: f64) -> (f64, f64) {
        if tr == 0.0 {
            (0.0, 0.0)
        } else {
            (100.0 * (pdm / tr), 100.0 * (mdm / tr))
        }
    }

    fn dx(plus_di: f64, minus_di: f64) -> f64 {
        let denom = plus_di + minus_di;
        if denom == 0.0 {
            0.0
        } else {
            100.0 * (plus_di - minus_di).abs() / denom
        }
    }

    fn step_adx(&mut self, dx: f64, plus_di: f64, minus_di: f64) -> AdxOutput {
        let adx = match self.adx {
            None => {
                self.dx_seed += dx;
                self.dx_count += 1;
                if self.dx_count < self.period {
                    None
                } else {
                    Some(self.dx_seed / self.period_f)
                }
            }
            Some(prev) => Some((prev * (self.period_f - 1.0) + dx) / self.period_f),
        };
        self.adx = adx;
        AdxOutput {
            adx,
            plus_di: Some(plus_di),
            minus_di: Some(minus_di),
        }
    }
}

impl Indicator for Adx {
    type Input = Ohlcv;
    type Output = AdxOutput;

    fn update(&mut self, bar: Ohlcv) -> Result<AdxOutput, IndicatorError> {
        check_bar(&bar)?;
        let current = Prev {
            high: bar.high,
            low: bar.low,
            close: bar.close,
        };
        let Some(prev) = self.prev.replace(current) else {
            return Ok(AdxOutput::default());
        };

        let tr = true_range(&bar, Some(prev.close));
        let up = bar.high - prev.high;
        let down = prev.low - bar.low;
        let pdm = if up > down && up > 0.0 { up } else { 0.0 };
        let mdm = if down > up && down > 0.0 { down } else { 0.0 };
        self.deltas += 1;

        let (tr_s, pdm_s, mdm_s) = match self.smoothed {
            None => {
                self.seed_tr += tr;
                self.seed_pdm += pdm;
                self.seed_mdm += mdm;
                if self.deltas < self.period {
                    return Ok(AdxOutput::default());
                }
                (self.seed_tr, self.seed_pdm, self.seed_mdm)
            }
            Some((t, p, m)) => (
                t - (t / self.period_f) + tr,
                p - (p / self.period_f) + pdm,
                m - (m / self.period_f) + mdm,
            ),
        };
        self.smoothed = Some((tr_s, pdm_s, mdm_s));

        let (plus_di, minus_di) = Self::di(tr_s, pdm_s, mdm_s);
        let dx = Self::dx(plus_di, minus_di);
        Ok(self.step_adx(dx, plus_di, minus_di))
    }

    fn is_ready(&self) -> bool {
        self.adx.is_some()
    }

    fn reset(&mut self) {
        *self = Self {
            period: self.period,
            period_f: self.period_f,
            prev: None,
            seed_tr: 0.0,
            seed_pdm: 0.0,
            seed_mdm: 0.0,
            deltas: 0,
            smoothed: None,
            dx_seed: 0.0,
            dx_count: 0,
            adx: None,
        };
    }

    fn warmup_periods(&self) -> usize {
        2 * self.period
    }
}

#[cfg(test)]
mod tests {
    use super::super::test_support::hlc;
    use super::*;

    #[test]
    fn warm_up_staging_matches_python() {
        let period = 3;
        let mut adx = Adx::new(period).unwrap();
        let bars: Vec<Ohlcv> = (0..10)
            .map(|i| {
                let base = 100.0 + f64::from(i) * 1.5;
                hlc(base + 1.0, base - 1.0, base + 0.5)
            })
            .collect();
        let out = adx.batch(&bars).unwrap();
        // Bars 0..=2: nothing (first bar records, then 2 seed deltas).
        for o in &out[..3] {
            assert_eq!(*o, AdxOutput::default());
        }
        // Bars 3 and 4: DI present, ADX still seeding (period DX values needed).
        for o in &out[3..5] {
            assert!(o.plus_di.is_some() && o.adx.is_none());
        }
        // First ADX on bar index 2 * period - 1 = 5, i.e. the 2 * period-th bar.
        assert!(out[5].adx.is_some());
        assert_eq!(adx.warmup_periods(), 6);
        // A steady uptrend: all movement is +DM, so DX = ADX = 100.
        assert_eq!(out[9].adx, Some(100.0));
        assert_eq!(out[9].minus_di, Some(0.0));
    }

    #[test]
    fn flat_seed_gives_zero_di_and_zero_dx() {
        let mut adx = Adx::new(2).unwrap();
        let flat = hlc(5.0, 5.0, 5.0);
        let out = adx.batch(&[flat, flat, flat, flat]).unwrap();
        assert_eq!(
            out[3],
            AdxOutput {
                adx: Some(0.0),
                plus_di: Some(0.0),
                minus_di: Some(0.0)
            }
        );
        adx.reset();
        assert!(!adx.is_ready());
    }
}
