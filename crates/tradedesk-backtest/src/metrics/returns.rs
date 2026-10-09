//! Return statistics over a daily simple-return series.
//!
//! The headline is the annualised Sharpe, `sqrt(252) × mean(excess) / std(excess)` with
//! the sample standard deviation (ddof = 1), where `excess = r − rf / 252`. It is always
//! read next to `observations`. The per-observation moments are exposed for the sweep's
//! deflated Sharpe (#33); volatility and Sortino are secondary.

use serde::{Deserialize, Serialize};

/// Trading days per year used to annualise daily figures.
pub const PERIODS_PER_YEAR: f64 = 252.0;

/// The ddof of every standard deviation in [`ReturnStats`].
pub const STD_DDOF: u32 = 1;

/// Statistics of a daily simple-return series. Every figure is `None` when it is
/// undefined: fewer than two observations, zero variance, no downside, or a series that
/// passed through ruin.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReturnStats {
    /// Daily observations behind every figure. A short series gives an imprecise Sharpe.
    pub observations: usize,
    /// Mean daily excess return, `mean(r − rf / 252)`.
    pub mean_excess: Option<f64>,
    /// Sample standard deviation (ddof 1) of the daily returns.
    pub std: Option<f64>,
    /// Per-observation Sharpe, `mean_excess / std`: not annualised. #33's deflated
    /// Sharpe works on this scale.
    pub sharpe_daily: Option<f64>,
    /// Annualised Sharpe, `sqrt(252) × sharpe_daily`. The headline figure.
    pub sharpe: Option<f64>,
    /// Biased sample skewness of the daily returns (`m3 / m2^1.5`).
    pub skewness: Option<f64>,
    /// Biased Pearson (non-excess) kurtosis of the daily returns (`m4 / m2²`; 3 for a
    /// normal).
    pub kurtosis: Option<f64>,
    /// Secondary: annualised volatility, `std × sqrt(252)`.
    pub volatility: Option<f64>,
    /// Secondary: annualised Sortino, `sqrt(252) × mean_excess / sqrt(mean(min(excess,
    /// 0)²))`, the downside deviation taken over every observation.
    pub sortino: Option<f64>,
}

impl ReturnStats {
    /// Every figure undefined, with the observation count kept.
    #[must_use]
    pub fn undefined(observations: usize) -> Self {
        Self {
            observations,
            mean_excess: None,
            std: None,
            sharpe_daily: None,
            sharpe: None,
            skewness: None,
            kurtosis: None,
            volatility: None,
            sortino: None,
        }
    }
}

#[allow(clippy::cast_precision_loss)]
fn mean(xs: &[f64]) -> f64 {
    xs.iter().sum::<f64>() / xs.len() as f64
}

/// Statistics of `returns` (daily simple returns) against an annual `risk_free_rate`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn return_stats(returns: &[f64], risk_free_rate: f64) -> ReturnStats {
    let n = returns.len();
    let mut stats = ReturnStats::undefined(n);
    if n == 0 || returns.iter().any(|r| !r.is_finite()) {
        return stats;
    }
    let rf_daily = risk_free_rate / PERIODS_PER_YEAR;
    let excess: Vec<f64> = returns.iter().map(|r| r - rf_daily).collect();
    let mean_excess = mean(&excess);
    stats.mean_excess = Some(mean_excess);
    if n < 2 {
        return stats;
    }
    // A constant series has zero variance, even where the float mean leaves a residue.
    #[allow(clippy::float_cmp)] // exact equality is the test
    let constant = returns.iter().all(|r| *r == returns[0]);
    let mean_r = mean(returns);
    let deviations: Vec<f64> = returns.iter().map(|r| r - mean_r).collect();
    let ss: f64 = deviations.iter().map(|d| d * d).sum();
    let std = if constant {
        0.0
    } else {
        (ss / (n - 1) as f64).sqrt()
    };
    stats.std = Some(std);
    stats.volatility = Some(std * PERIODS_PER_YEAR.sqrt());
    if std > 0.0 {
        let sharpe_daily = mean_excess / std;
        stats.sharpe_daily = Some(sharpe_daily);
        stats.sharpe = Some(sharpe_daily * PERIODS_PER_YEAR.sqrt());
        let m2 = ss / n as f64;
        let m3 = mean(&deviations.iter().map(|d| d.powi(3)).collect::<Vec<_>>());
        let m4 = mean(&deviations.iter().map(|d| d.powi(4)).collect::<Vec<_>>());
        stats.skewness = Some(m3 / m2.powf(1.5));
        stats.kurtosis = Some(m4 / (m2 * m2));
    }
    let downside_sq = mean(
        &excess
            .iter()
            .map(|e| e.min(0.0).powi(2))
            .collect::<Vec<_>>(),
    );
    if downside_sq > 0.0 {
        stats.sortino = Some(PERIODS_PER_YEAR.sqrt() * mean_excess / downside_sq.sqrt());
    }
    stats
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn hand_computed_series() {
        // r = [0.01, -0.01, 0.02]: mean 0.00667, sample var = (0.0000111+0.0002778+0.0001778)/2.
        let s = return_stats(&[0.01, -0.01, 0.02], 0.0);
        assert_eq!(s.observations, 3);
        let m: f64 = 0.02 / 3.0;
        let var = ((0.01 - m).powi(2) + (-0.01 - m).powi(2) + (0.02 - m).powi(2)) / 2.0_f64;
        assert!((s.mean_excess.unwrap() - m).abs() < 1e-15);
        assert!((s.std.unwrap() - var.sqrt()).abs() < 1e-15);
        assert!((s.sharpe.unwrap() - 252f64.sqrt() * m / var.sqrt()).abs() < 1e-12);
        // Downside: only -0.01 contributes, over all three observations.
        let dd = (0.0001f64 / 3.0).sqrt();
        assert!((s.sortino.unwrap() - 252f64.sqrt() * m / dd).abs() < 1e-12);
    }

    #[test]
    fn short_or_flat_series_have_no_sharpe() {
        assert_eq!(return_stats(&[], 0.0), ReturnStats::undefined(0));
        let one = return_stats(&[0.01], 0.0);
        assert_eq!(one.observations, 1);
        assert_eq!(one.mean_excess, Some(0.01));
        assert_eq!(one.sharpe, None);
        let flat = return_stats(&[0.1; 5], 0.0);
        assert_eq!(flat.std, Some(0.0));
        assert_eq!(
            (flat.sharpe, flat.skewness, flat.sortino),
            (None, None, None)
        );
        // No losing day: no downside deviation, so no Sortino; the Sharpe is defined.
        let up = return_stats(&[0.01, 0.02], 0.0);
        assert!(up.sharpe.is_some());
        assert_eq!(up.sortino, None);
        assert_eq!(
            return_stats(&[0.01, f64::NAN], 0.0),
            ReturnStats::undefined(2)
        );
    }

    #[test]
    fn the_risk_free_rate_shifts_the_mean_not_the_std() {
        let r = [0.01, -0.005, 0.002, 0.004];
        let a = return_stats(&r, 0.0);
        let b = return_stats(&r, 0.0504);
        assert!((a.mean_excess.unwrap() - b.mean_excess.unwrap() - 0.0002).abs() < 1e-15);
        assert_eq!(a.std, b.std);
        assert!(b.sharpe.unwrap() < a.sharpe.unwrap());
    }

    proptest! {
        /// The Sharpe's sign is the mean excess return's sign.
        #[test]
        fn sharpe_sign_follows_the_mean(
            returns in prop::collection::vec(-0.05f64..0.05, 2..200),
            rf in -0.05f64..0.1,
        ) {
            let s = return_stats(&returns, rf);
            if let Some(sharpe) = s.sharpe {
                let m = s.mean_excess.unwrap();
                prop_assert_eq!(sharpe > 0.0, m > 0.0);
                prop_assert_eq!(sharpe < 0.0, m < 0.0);
            } else {
                prop_assert!(returns.iter().all(|r| *r == returns[0]));
            }
        }
    }
}
