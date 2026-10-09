//! The probability of backtest overfitting by combinatorially symmetric
//! cross-validation (Bailey, Borwein, López de Prado & Zhu, "The Probability of Backtest
//! Overfitting", *Journal of Computational Finance* 20(4), 2017).
//!
//! Given `N` trials' returns on the same `T` dates, the rows are split into `S`
//! contiguous blocks. For each of the `C(S, S/2)` ways to choose half the blocks as the
//! in-sample (IS) set, the other half is out-of-sample (OOS):
//!
//! 1. each trial's Sharpe (mean / sample std, ddof 1) is computed on both sets; it is
//!    undefined (NaN) with fewer than two rows or zero variance, and a partition where
//!    every IS or every OOS Sharpe is undefined is skipped;
//! 2. `n*` is the IS-best trial (undefined Sharpes skipped, the first on a tie);
//! 3. its OOS rank among all trials (ties averaged, undefined lowest) gives
//!    `ω = rank / (N + 1)`, clamped to `[1e-12, 1 − 1e-12]`, and the logit
//!    `λ = ln(ω / (1 − ω))`.
//!
//! **PBO is the share of logits `λ ≤ 0`**: how often the in-sample winner ranks at or
//! below the out-of-sample median.
//!
//! This is the Python reference implementation's `cscv.py::pbo_cscv` (the source of the
//! goldens in `tests/fixtures/overfit_golden.json`) step for step (block
//! sizes as `np.array_split`, combinations in `itertools.combinations` order, the
//! sums in row order as numpy reduces along axis 0), with one default changed: the
//! Python purges by default (`label_horizon = 1`), while here both the purge and the
//! embargo default to 0, which is the paper's plain CSCV. Daily returns on the equity
//! curve do not overlap, so there is nothing to purge; the Python rule is available
//! through [`CscvConfig`].

use rayon::prelude::*;
use serde::{Deserialize, Serialize};

/// CSCV parameters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CscvConfig {
    /// `S`, the number of contiguous blocks: even and at least 2. `C(S, S/2)`
    /// partitions are evaluated (12 870 at the default 16).
    pub blocks: usize,
    /// Purge: a training row `t` is dropped when a test row lies in `[t, t + h]`.
    pub label_horizon: usize,
    /// Embargo: a training row `t` is dropped when a test row lies in `[t − e, t)`.
    pub embargo: usize,
}

impl Default for CscvConfig {
    /// `S = 16`, no purge, no embargo.
    fn default() -> Self {
        Self {
            blocks: 16,
            label_horizon: 0,
            embargo: 0,
        }
    }
}

/// Why CSCV cannot run on a matrix.
#[derive(Debug, Clone, PartialEq, Eq, thiserror::Error)]
pub enum CscvError {
    /// `S` is odd or below 2.
    #[error("blocks (S) must be an even integer >= 2, got {0}")]
    Blocks(usize),
    /// Fewer than two trials to rank.
    #[error("need at least 2 trials to rank, got {0}")]
    TooFewTrials(usize),
    /// Fewer observations than blocks.
    #[error("need at least {blocks} observations (one per block), got {observations}")]
    TooFewObservations {
        /// `S`.
        blocks: usize,
        /// `T`.
        observations: usize,
    },
    /// The trials' series differ in length.
    #[error("trial {trial} has {got} observations, trial 0 has {expected}")]
    Ragged {
        /// The offending trial's column.
        trial: usize,
        /// Its length.
        got: usize,
        /// The first trial's length.
        expected: usize,
    },
    /// A return is NaN or infinite.
    #[error("trial {trial} has a non-finite return at observation {row}")]
    NonFinite {
        /// Column.
        trial: usize,
        /// Row.
        row: usize,
    },
}

/// What CSCV found.
#[derive(Debug, Clone, PartialEq)]
pub struct CscvResult {
    /// The share of logits `≤ 0`; `None` when every partition was skipped.
    pub pbo: Option<f64>,
    /// The parameters used.
    pub config: CscvConfig,
    /// Partitions evaluated (`C(S, S/2)` less any skipped).
    pub combinations: usize,
    /// The IS-best trial's OOS logit for each evaluated partition, in combination order.
    pub logits: Vec<f64>,
}

/// Every `k`-subset of `0..n` in lexicographic order (`itertools.combinations`).
fn combinations(n: usize, k: usize) -> Vec<Vec<usize>> {
    let mut out = Vec::new();
    let mut c: Vec<usize> = (0..k).collect();
    loop {
        out.push(c.clone());
        // The rightmost index that can still move right.
        let Some(i) = (0..k).rev().find(|&i| c[i] != i + n - k) else {
            return out;
        };
        c[i] += 1;
        for j in i + 1..k {
            c[j] = c[j - 1] + 1;
        }
    }
}

/// `np.array_split(np.arange(t), s)`: `t % s` blocks of `t / s + 1` rows, then blocks
/// of `t / s`.
fn block_bounds(t: usize, s: usize) -> Vec<std::ops::Range<usize>> {
    let (size, extra) = (t / s, t % s);
    let mut start = 0;
    (0..s)
        .map(|b| {
            let len = size + usize::from(b < extra);
            let r = start..start + len;
            start += len;
            r
        })
        .collect()
}

/// Per-column Sharpe over `rows`, NaN with fewer than two rows or zero std. The sums run
/// in row order, as numpy's axis-0 reduction does.
#[allow(clippy::cast_precision_loss)]
fn sharpe_per_column(columns: &[Vec<f64>], rows: &[usize]) -> Vec<f64> {
    let n = rows.len();
    columns
        .iter()
        .map(|col| {
            if n < 2 {
                return f64::NAN;
            }
            let mut sum = 0.0;
            for &r in rows {
                sum += col[r];
            }
            let mean = sum / n as f64;
            let mut ss = 0.0;
            for &r in rows {
                let d = col[r] - mean;
                ss += d * d;
            }
            let sd = (ss / (n - 1) as f64).sqrt();
            if sd > 0.0 { mean / sd } else { f64::NAN }
        })
        .collect()
}

/// The first index of the largest non-NaN value (`np.nanargmax`).
fn nan_argmax(xs: &[f64]) -> Option<usize> {
    let mut best: Option<usize> = None;
    for (i, &x) in xs.iter().enumerate() {
        if !x.is_nan() && best.is_none_or(|b| x > xs[b]) {
            best = Some(i);
        }
    }
    best
}

/// The average rank (1-based) of `xs[i]` among `xs`, NaN counted as `−∞`
/// (`scipy.stats.rankdata(np.nan_to_num(x, nan=-inf))[i]`).
#[allow(clippy::cast_precision_loss)]
fn average_rank(xs: &[f64], i: usize) -> f64 {
    let key = |x: f64| if x.is_nan() { f64::NEG_INFINITY } else { x };
    let v = key(xs[i]);
    let below = xs.iter().filter(|&&x| key(x) < v).count();
    #[allow(clippy::float_cmp)] // ties are exact equality, as in rankdata
    let equal = xs.iter().filter(|&&x| key(x) == v).count();
    below as f64 + (equal as f64 + 1.0) / 2.0
}

/// The logit of the IS-best trial's OOS rank for one partition, `None` when skipped.
#[allow(clippy::cast_precision_loss)]
fn partition_logit(columns: &[Vec<f64>], train: &[usize], test: &[usize]) -> Option<f64> {
    let is = sharpe_per_column(columns, train);
    let oos = sharpe_per_column(columns, test);
    if is.iter().all(|x| x.is_nan()) || oos.iter().all(|x| x.is_nan()) {
        return None;
    }
    let best = nan_argmax(&is)?;
    let omega = (average_rank(&oos, best) / (columns.len() + 1) as f64).clamp(1e-12, 1.0 - 1e-12);
    Some((omega / (1.0 - omega)).ln())
}

/// Run CSCV over `columns`, one per trial, each the trial's returns on the same dates.
///
/// # Errors
/// [`CscvError`] for an odd or too-small `S`, fewer than two trials, fewer observations
/// than blocks, ragged columns or a non-finite return.
#[allow(clippy::cast_precision_loss)]
pub fn pbo_cscv(columns: &[Vec<f64>], config: CscvConfig) -> Result<CscvResult, CscvError> {
    let s = config.blocks;
    if s < 2 || s % 2 != 0 {
        return Err(CscvError::Blocks(s));
    }
    if columns.len() < 2 {
        return Err(CscvError::TooFewTrials(columns.len()));
    }
    let t = columns[0].len();
    for (trial, col) in columns.iter().enumerate() {
        if col.len() != t {
            return Err(CscvError::Ragged {
                trial,
                got: col.len(),
                expected: t,
            });
        }
        if let Some(row) = col.iter().position(|x| !x.is_finite()) {
            return Err(CscvError::NonFinite { trial, row });
        }
    }
    if t < s {
        return Err(CscvError::TooFewObservations {
            blocks: s,
            observations: t,
        });
    }

    let blocks = block_bounds(t, s);
    let logits: Vec<f64> = combinations(s, s / 2)
        .par_iter()
        .map(|train_ids| {
            let mut in_train = vec![false; s];
            for &b in train_ids {
                in_train[b] = true;
            }
            let rows = |train: bool| -> Vec<usize> {
                (0..s)
                    .filter(|&b| in_train[b] == train)
                    .flat_map(|b| blocks[b].clone())
                    .collect()
            };
            let test = rows(false);
            let mut train = rows(true);
            // Purge and embargo: block training rows within [s − h, s + e] of a test row.
            let mut blocked = vec![false; t];
            for &r in &test {
                let lo = r.saturating_sub(config.label_horizon);
                let hi = (r + config.embargo).min(t - 1);
                for flag in &mut blocked[lo..=hi] {
                    *flag = true;
                }
            }
            train.retain(|&r| !blocked[r]);
            partition_logit(columns, &train, &test)
        })
        .collect::<Vec<_>>()
        .into_iter()
        .flatten()
        .collect();

    let pbo = if logits.is_empty() {
        None
    } else {
        Some(logits.iter().filter(|&&l| l <= 0.0).count() as f64 / logits.len() as f64)
    };
    Ok(CscvResult {
        pbo,
        config,
        combinations: logits.len(),
        logits,
    })
}

/// A distribution summary of the logits: count, mean, sample std (ddof 1), min,
/// quartiles (numpy's default linear interpolation) and max.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct LogitSummary {
    /// Number of logits.
    pub count: usize,
    /// Mean.
    pub mean: f64,
    /// Sample standard deviation (ddof 1); `None` below two logits.
    pub std: Option<f64>,
    /// Smallest.
    pub min: f64,
    /// 25th percentile.
    pub q25: f64,
    /// Median.
    pub median: f64,
    /// 75th percentile.
    pub q75: f64,
    /// Largest.
    pub max: f64,
}

impl LogitSummary {
    /// Summarise `logits`; `None` when empty.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn of(logits: &[f64]) -> Option<Self> {
        let n = logits.len();
        if n == 0 {
            return None;
        }
        let mut sorted = logits.to_vec();
        sorted.sort_by(f64::total_cmp);
        let quantile = |q: f64| {
            let pos = q * (n - 1) as f64;
            #[allow(clippy::cast_possible_truncation, clippy::cast_sign_loss)]
            let lo = pos.floor() as usize;
            let hi = (lo + 1).min(n - 1);
            let frac = pos - lo as f64;
            sorted[lo] + (sorted[hi] - sorted[lo]) * frac
        };
        let mean = logits.iter().sum::<f64>() / n as f64;
        let std = (n > 1).then(|| {
            let ss: f64 = logits.iter().map(|l| (l - mean) * (l - mean)).sum();
            (ss / (n - 1) as f64).sqrt()
        });
        Some(Self {
            count: n,
            mean,
            std,
            min: sorted[0],
            q25: quantile(0.25),
            median: quantile(0.5),
            q75: quantile(0.75),
            max: sorted[n - 1],
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    #[test]
    fn combinations_follow_itertools_order() {
        assert_eq!(
            combinations(4, 2),
            vec![
                vec![0, 1],
                vec![0, 2],
                vec![0, 3],
                vec![1, 2],
                vec![1, 3],
                vec![2, 3]
            ]
        );
        assert_eq!(combinations(16, 8).len(), 12_870);
    }

    #[test]
    fn blocks_split_like_array_split() {
        let b = block_bounds(10, 4);
        assert_eq!(b, vec![0..3, 3..6, 6..8, 8..10]);
    }

    #[test]
    fn ranks_average_ties_and_put_undefined_lowest() {
        let xs = [0.5, f64::NAN, 0.5, 0.9, f64::NAN];
        assert_eq!(average_rank(&xs, 0), 3.5);
        assert_eq!(average_rank(&xs, 1), 1.5);
        assert_eq!(average_rank(&xs, 3), 5.0);
        assert_eq!(nan_argmax(&[f64::NAN, 0.2, 0.7, 0.7]), Some(2));
        assert_eq!(nan_argmax(&[f64::NAN]), None);
    }

    #[test]
    fn a_trial_better_everywhere_is_never_overfit() {
        // Trial 1 is trial 0 shifted up: its Sharpe is higher on every subset, so it
        // wins in sample and ranks top out of sample every time.
        let base: Vec<f64> = (0..32).map(|i| f64::from(i % 5) * 0.01 - 0.015).collect();
        let better: Vec<f64> = base.iter().map(|r| r + 0.02).collect();
        let r = pbo_cscv(&[base, better], CscvConfig::default()).unwrap();
        assert_eq!(r.combinations, 12_870);
        assert_eq!(r.pbo, Some(0.0));
        assert!(
            r.logits
                .iter()
                .all(|&l| (l - (2.0_f64 / 3.0 / (1.0 / 3.0)).ln()).abs() < 1e-12)
        );
    }

    #[test]
    fn invalid_inputs_are_refused() {
        let col = vec![0.01; 20];
        let cfg = |blocks| CscvConfig {
            blocks,
            ..CscvConfig::default()
        };
        assert_eq!(
            pbo_cscv(&[col.clone(), col.clone()], cfg(3)),
            Err(CscvError::Blocks(3))
        );
        assert_eq!(
            pbo_cscv(&[col.clone()], cfg(4)),
            Err(CscvError::TooFewTrials(1))
        );
        assert!(matches!(
            pbo_cscv(&[col.clone(), col[..10].to_vec()], cfg(4)),
            Err(CscvError::Ragged { .. })
        ));
        assert!(matches!(
            pbo_cscv(&[col.clone(), col.clone()], cfg(22)),
            Err(CscvError::TooFewObservations { .. })
        ));
        let mut bad = col.clone();
        bad[3] = f64::NAN;
        assert_eq!(
            pbo_cscv(&[col, bad], cfg(4)),
            Err(CscvError::NonFinite { trial: 1, row: 3 })
        );
    }

    #[test]
    fn constant_trials_skip_every_partition() {
        let r = pbo_cscv(
            &[vec![0.0; 8], vec![0.0; 8]],
            CscvConfig {
                blocks: 4,
                ..CscvConfig::default()
            },
        )
        .unwrap();
        assert_eq!(r.pbo, None);
        assert_eq!(r.combinations, 0);
    }

    #[test]
    fn logit_summary_matches_numpy_percentiles() {
        let s = LogitSummary::of(&[4.0, 1.0, 3.0, 2.0]).unwrap();
        assert_eq!(
            (s.min, s.q25, s.median, s.q75, s.max),
            (1.0, 1.75, 2.5, 3.25, 4.0)
        );
        assert_eq!(s.mean, 2.5);
        assert!((s.std.unwrap() - (5.0_f64 / 3.0).sqrt()).abs() < 1e-15);
        assert_eq!(LogitSummary::of(&[]), None);
    }

    fn matrix() -> impl Strategy<Value = Vec<Vec<f64>>> {
        (2_usize..6, 8_usize..40).prop_flat_map(|(n, t)| {
            prop::collection::vec(prop::collection::vec(-0.05_f64..0.05, t), n)
        })
    }

    proptest! {
        #![proptest_config(ProptestConfig::with_cases(64))]
        #[test]
        fn pbo_is_a_probability(columns in matrix(), half in 1_usize..4, h in 0_usize..3, e in 0_usize..3) {
            let config = CscvConfig { blocks: 2 * half, label_horizon: h, embargo: e };
            let r = pbo_cscv(&columns, config).unwrap();
            if let Some(p) = r.pbo {
                prop_assert!((0.0..=1.0).contains(&p));
            }
            prop_assert!(r.logits.iter().all(|l| l.is_finite()));
        }
    }
}
