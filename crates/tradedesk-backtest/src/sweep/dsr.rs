//! The deflated Sharpe ratio (Bailey & López de Prado, 2014, "The Deflated Sharpe
//! Ratio: Correcting for Selection Bias, Backtest Overfitting and Non-Normality",
//! *Journal of Portfolio Management* 40(5)).
//!
//! Every quantity is on the **per-observation** (daily) scale: the Sharpe is
//! `ReturnStats::sharpe_daily`, never the annualised figure. Annualisation would cancel
//! in the PSR but inflate the deflation benchmark if the scales were mixed.
//!
//! ```text
//! PSR(SR*) = Φ( (SR̂ − SR*) · √(T − 1) / √(1 − γ₃·SR̂ + (γ₄ − 1)/4 · SR̂²) )
//! SR₀      = σ_SR · [ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]
//! DSR      = PSR(SR₀)
//! ```
//!
//! `T` is the number of daily observations, `γ₃` the skewness and `γ₄` the Pearson
//! (non-excess) kurtosis of the trial's returns, `N` the number of trials, `σ_SR` the
//! standard deviation of the trials' Sharpes, and `γ` the Euler–Mascheroni constant.
//! This follows the paper. The two points it leaves open are settled as follows: the
//! variance term is floored at `1e-12` (an extreme skew/kurtosis then degrades to a
//! near-certain answer rather than an error), and with `N ≤ 1` or `σ_SR ≤ 0` there is
//! no multiple-testing burden and `SR₀ = 0`.

use statrs::distribution::{ContinuousCDF, Normal};

/// The Euler–Mascheroni constant, the Gumbel correction in the expected maximum.
pub const EULER_MASCHERONI: f64 = 0.577_215_664_901_532_9;

fn standard_normal() -> Normal {
    Normal::standard()
}

/// The probability that the true Sharpe exceeds `benchmark` (both per observation),
/// given `n_obs` observations with the given skewness and Pearson kurtosis. `None` below
/// two observations or for a non-finite input.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn probabilistic_sharpe_ratio(
    sharpe: f64,
    benchmark: f64,
    n_obs: usize,
    skewness: f64,
    kurtosis: f64,
) -> Option<f64> {
    if n_obs < 2
        || ![sharpe, benchmark, skewness, kurtosis]
            .iter()
            .all(|x| x.is_finite())
    {
        return None;
    }
    let mut variance = 1.0 - skewness * sharpe + (kurtosis - 1.0) / 4.0 * sharpe * sharpe;
    if variance <= 0.0 {
        variance = 1e-12;
    }
    let z = (sharpe - benchmark) * ((n_obs - 1) as f64).sqrt() / variance.sqrt();
    Some(standard_normal().cdf(z))
}

/// `SR₀`, the expected maximum per-observation Sharpe of `n_trials` independent trials
/// whose true Sharpe is zero and whose estimated Sharpes have standard deviation
/// `sharpe_std`. Zero for `n_trials ≤ 1` or a non-positive (or non-finite) `sharpe_std`.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn expected_max_sharpe(n_trials: u64, sharpe_std: f64) -> f64 {
    if n_trials <= 1 || !(sharpe_std.is_finite() && sharpe_std > 0.0) {
        return 0.0;
    }
    let n = n_trials as f64;
    let normal = standard_normal();
    let z1 = normal.inverse_cdf(1.0 - 1.0 / n);
    let z2 = normal.inverse_cdf(1.0 - 1.0 / (n * std::f64::consts::E));
    sharpe_std * ((1.0 - EULER_MASCHERONI) * z1 + EULER_MASCHERONI * z2)
}

/// The deflated Sharpe ratio: [`probabilistic_sharpe_ratio`] against
/// [`expected_max_sharpe`]`(n_trials, sharpe_std)`.
#[must_use]
pub fn deflated_sharpe_ratio(
    sharpe: f64,
    n_obs: usize,
    n_trials: u64,
    sharpe_std: f64,
    skewness: f64,
    kurtosis: f64,
) -> Option<f64> {
    probabilistic_sharpe_ratio(
        sharpe,
        expected_max_sharpe(n_trials, sharpe_std),
        n_obs,
        skewness,
        kurtosis,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn no_burden_means_a_zero_benchmark() {
        assert_eq!(expected_max_sharpe(1, 0.3), 0.0);
        assert_eq!(expected_max_sharpe(10, 0.0), 0.0);
        assert_eq!(expected_max_sharpe(10, f64::NAN), 0.0);
        // N = 2: Φ⁻¹(1/2) = 0, so only the γ term remains.
        let z = Normal::standard().inverse_cdf(1.0 - 1.0 / (2.0 * std::f64::consts::E));
        assert!((expected_max_sharpe(2, 1.0) - EULER_MASCHERONI * z).abs() < 1e-15);
    }

    #[test]
    fn psr_is_a_half_at_the_benchmark_and_undefined_below_two_observations() {
        assert_eq!(
            probabilistic_sharpe_ratio(0.1, 0.1, 100, 0.0, 3.0),
            Some(0.5)
        );
        assert_eq!(probabilistic_sharpe_ratio(0.1, 0.0, 1, 0.0, 3.0), None);
        // A degenerate variance term is floored, not an error.
        let p = probabilistic_sharpe_ratio(1.0, 0.0, 10, 10.0, 1.0).unwrap();
        assert!(p > 0.999_999);
    }

    proptest! {
        #[test]
        fn dsr_is_a_probability_that_never_rises_with_more_trials(
            sharpe in -0.5_f64..0.5,
            n_obs in 2_usize..2000,
            sigma in 0.0_f64..0.5,
            skew in -3.0_f64..3.0,
            kurt in 1.0_f64..30.0,
            n in 1_u64..5000,
            more in 1_u64..5000,
        ) {
            let a = deflated_sharpe_ratio(sharpe, n_obs, n, sigma, skew, kurt).unwrap();
            let b = deflated_sharpe_ratio(sharpe, n_obs, n + more, sigma, skew, kurt).unwrap();
            prop_assert!((0.0..=1.0).contains(&a) && (0.0..=1.0).contains(&b));
            // A hair of slack for the CDF's own rounding.
            prop_assert!(b <= a + 1e-12, "DSR rose from {a} to {b} as N grew from {n} by {more}");
            prop_assert!(expected_max_sharpe(n + more, sigma) >= expected_max_sharpe(n, sigma));
        }
    }
}
