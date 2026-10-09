//! The deflated Sharpe and PBO across one sweep, computed from its registry records.
//!
//! - **Trials counted.** `N` for the deflated Sharpe is the number of completed trials
//!   (ruined and no-trade trials included: they were tried) plus
//!   [`ReportOptions::prior_trials`] for a cumulative count; failed cells are not
//!   trials. `σ_SR` is the sample std (ddof 1) of the completed trials' defined daily
//!   Sharpes.
//! - **PBO matrix.** One column per completed, non-ruined trial: its
//!   `Metrics::daily_returns()`. Every trial of a sweep shares the window and so the
//!   same dates: the window's weekdays, plus its weekend close when the window ends on
//!   a Saturday or Sunday (#54). The report checks that and refuses misaligned dates.
//! - A trial id seen twice (a sweep re-run into the same file) counts once.

use std::collections::{BTreeMap, BTreeSet};

use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::cscv::{CscvConfig, LogitSummary, pbo_cscv};
use super::dsr::{deflated_sharpe_ratio, expected_max_sharpe};
use super::registry::{RegistryRecord, TrialOutcome, TrialRecord};
use crate::metrics::PERIODS_PER_YEAR;

/// How the report is computed.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub struct ReportOptions {
    /// CSCV parameters (default `S = 16`, no purge, no embargo).
    pub cscv: CscvConfig,
    /// Trials run before this sweep on the same question, added to `N`.
    pub prior_trials: u64,
}

/// Why a report could not be computed.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum ReportError {
    /// The records hold no sweep.
    #[error("the registry holds no sweep")]
    NoSweep,
    /// The records hold several sweeps and none was chosen.
    #[error("the registry holds {0} sweeps; choose one by id")]
    SeveralSweeps(usize),
    /// The chosen sweep is not in the records.
    #[error("sweep {0} is not in the registry")]
    UnknownSweep(String),
    /// Two trials' daily returns are on different dates.
    #[error("trial {trial_id} has daily returns on different dates from trial {first}")]
    MisalignedDates {
        /// The first trial's id.
        first: String,
        /// The misaligned trial's id.
        trial_id: String,
    },
}

/// One trial's deflated Sharpe.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TrialDsr {
    /// Trial id.
    pub trial_id: String,
    /// Cell index.
    pub index: usize,
    /// Instrument.
    pub instrument: String,
    /// The cell's parameters.
    pub params: BTreeMap<String, Value>,
    /// Daily observations.
    pub observations: usize,
    /// Per-observation Sharpe; `None` when undefined (no variance, ruin).
    pub sharpe_daily: Option<f64>,
    /// Annualised Sharpe, `√252 × sharpe_daily`.
    pub sharpe: Option<f64>,
    /// The deflated Sharpe ratio: the probability the true Sharpe exceeds `SR₀`.
    pub dsr: Option<f64>,
    /// Whether the trial may be reported as costed: its run metadata says so for its
    /// instrument ([`crate::RunMetadata::is_costed`]).
    #[serde(default)]
    pub costed: bool,
}

/// The best trial against the multiple-testing benchmark.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BestTrial {
    /// The trial (the highest daily Sharpe; the first on a tie).
    pub trial: TrialDsr,
    /// Raw annualised Sharpe, `√252 × SR̂`.
    pub sharpe: f64,
    /// The annualised benchmark, `√252 × SR₀`: the best Sharpe expected from `N`
    /// trials with no skill.
    pub benchmark: f64,
    /// The annualised Sharpe in excess of the benchmark, `√252 × (SR̂ − SR₀)`.
    pub deflated_sharpe: f64,
    /// The deflated Sharpe ratio.
    pub dsr: f64,
    /// `1 − DSR`: the probability that the best trial is a false discovery.
    pub false_discovery_probability: f64,
}

/// The deflated-Sharpe inputs shared by every trial.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct DsrBasis {
    /// `N`: completed trials plus `prior_trials`.
    pub trials: u64,
    /// Trials counted from before this sweep.
    pub prior_trials: u64,
    /// `σ_SR`, the sample std of the defined daily Sharpes; `None` below two.
    pub sharpe_std_daily: Option<f64>,
    /// `SR₀` per observation (0 without a multiple-testing burden).
    pub benchmark_daily: f64,
}

/// The PBO, or why it was not computed.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "snake_case")]
pub enum PboOutcome {
    /// CSCV ran.
    Computed(PboReport),
    /// Too few trials or observations, or every partition was skipped.
    NotComputed {
        /// Why.
        reason: String,
    },
}

/// CSCV's result.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PboReport {
    /// The parameters.
    pub cscv: CscvConfig,
    /// Trials in the matrix (completed, not ruined).
    pub trials: usize,
    /// Daily observations per trial.
    pub observations: usize,
    /// Partitions evaluated.
    pub combinations: usize,
    /// The probability of backtest overfitting.
    pub pbo: f64,
    /// The IS-best trial's OOS logits.
    pub logits: LogitSummary,
}

/// Deflated Sharpe and PBO across one sweep.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SweepReport {
    /// The sweep.
    pub sweep_id: String,
    /// Distinct trials recorded.
    pub trials: usize,
    /// Trials that completed.
    pub completed: usize,
    /// Cells recorded as failures.
    pub failed: usize,
    /// Completed trials that were ruined.
    pub ruined: usize,
    /// Whether the sweep may be reported as costed: at least one trial completed and
    /// every completed trial is costed. A sweep with an uncosted trial is not costed.
    #[serde(default)]
    pub costed: bool,
    /// The shared DSR inputs.
    pub dsr: DsrBasis,
    /// Every completed trial, in cell order.
    pub per_trial: Vec<TrialDsr>,
    /// The best completed trial with a defined Sharpe.
    pub best: Option<BestTrial>,
    /// The PBO.
    pub pbo: PboOutcome,
}

#[allow(clippy::cast_precision_loss)]
fn sample_std(xs: &[f64]) -> Option<f64> {
    let n = xs.len();
    if n < 2 {
        return None;
    }
    let mean = xs.iter().sum::<f64>() / n as f64;
    let ss: f64 = xs.iter().map(|x| (x - mean) * (x - mean)).sum();
    Some((ss / (n - 1) as f64).sqrt())
}

impl SweepReport {
    /// The report for `sweep_id` (or the only sweep in `records`).
    ///
    /// # Errors
    /// [`ReportError`] when the sweep cannot be identified or its trials' dates differ.
    pub fn from_records(
        records: &[RegistryRecord],
        sweep_id: Option<&str>,
        options: ReportOptions,
    ) -> Result<Self, ReportError> {
        let (sweep_id, trials) = sweep_trials(records, sweep_id)?;
        Self::compute(sweep_id, &trials, options)
    }

    #[allow(clippy::cast_precision_loss)]
    fn compute(
        sweep_id: String,
        trials: &[&TrialRecord],
        options: ReportOptions,
    ) -> Result<Self, ReportError> {
        let completed: Vec<&TrialRecord> = trials
            .iter()
            .copied()
            .filter(|t| matches!(t.outcome, TrialOutcome::Ok { .. }))
            .collect();
        let annualise = PERIODS_PER_YEAR.sqrt();
        let defined: Vec<f64> = completed
            .iter()
            .filter_map(|t| t.metrics().and_then(|m| m.returns.sharpe_daily))
            .collect();
        let n = completed.len() as u64 + options.prior_trials;
        let sharpe_std = sample_std(&defined);
        let benchmark = expected_max_sharpe(n, sharpe_std.unwrap_or(0.0));

        let per_trial: Vec<TrialDsr> = completed
            .iter()
            .map(|t| {
                let r = &t.metrics().expect("completed").returns;
                let dsr = match (r.sharpe_daily, r.skewness, r.kurtosis) {
                    (Some(sr), Some(skew), Some(kurt)) => deflated_sharpe_ratio(
                        sr,
                        r.observations,
                        n,
                        sharpe_std.unwrap_or(0.0),
                        skew,
                        kurt,
                    ),
                    _ => None,
                };
                TrialDsr {
                    trial_id: t.trial_id.clone(),
                    index: t.index,
                    instrument: t.instrument.clone(),
                    params: t.params.clone(),
                    observations: r.observations,
                    sharpe_daily: r.sharpe_daily,
                    sharpe: r.sharpe_daily.map(|s| s * annualise),
                    dsr,
                    costed: t.run.is_costed(&t.instrument),
                }
            })
            .collect();
        let best = per_trial
            .iter()
            .filter_map(|t| Some((t, t.sharpe_daily?, t.dsr?)))
            .fold(None::<(&TrialDsr, f64, f64)>, |best, cur| match best {
                Some(b) if b.1 >= cur.1 => Some(b),
                _ => Some(cur),
            })
            .map(|(t, sr, dsr)| BestTrial {
                trial: t.clone(),
                sharpe: sr * annualise,
                benchmark: benchmark * annualise,
                deflated_sharpe: (sr - benchmark) * annualise,
                dsr,
                false_discovery_probability: 1.0 - dsr,
            });

        let pbo = pbo_outcome(&completed, options.cscv)?;

        Ok(Self {
            sweep_id,
            trials: trials.len(),
            completed: completed.len(),
            failed: trials.len() - completed.len(),
            ruined: completed
                .iter()
                .filter(|t| t.metrics().is_some_and(|m| m.ruined_on.is_some()))
                .count(),
            costed: !per_trial.is_empty() && per_trial.iter().all(|t| t.costed),
            dsr: DsrBasis {
                trials: n,
                prior_trials: options.prior_trials,
                sharpe_std_daily: sharpe_std,
                benchmark_daily: benchmark,
            },
            per_trial,
            best,
            pbo,
        })
    }
}

/// The sweep `sweep_id` names (or the only sweep in `records`) and its distinct trials in
/// cell order: a trial id seen twice counts once.
pub(crate) fn sweep_trials<'a>(
    records: &'a [RegistryRecord],
    sweep_id: Option<&str>,
) -> Result<(String, Vec<&'a TrialRecord>), ReportError> {
    let sweeps: BTreeSet<&str> = records
        .iter()
        .map(|r| match r {
            RegistryRecord::Sweep(s) => s.sweep_id.as_str(),
            RegistryRecord::Trial(t) => t.sweep_id.as_str(),
        })
        .collect();
    let sweep_id = match sweep_id {
        Some(id) if sweeps.contains(id) => id.to_owned(),
        Some(id) => return Err(ReportError::UnknownSweep(id.to_owned())),
        None => match (sweeps.first(), sweeps.len()) {
            (Some(id), 1) => (*id).to_owned(),
            (None, _) => return Err(ReportError::NoSweep),
            (Some(_), n) => return Err(ReportError::SeveralSweeps(n)),
        },
    };
    let mut seen = BTreeSet::new();
    let mut trials: Vec<&TrialRecord> = records
        .iter()
        .filter_map(|r| match r {
            RegistryRecord::Trial(t) if t.sweep_id == sweep_id => Some(t.as_ref()),
            _ => None,
        })
        .filter(|t| seen.insert(t.trial_id.as_str()))
        .collect();
    trials.sort_by_key(|t| t.index);
    Ok((sweep_id, trials))
}

/// CSCV over the completed, non-ruined trials' daily returns.
fn pbo_outcome(completed: &[&TrialRecord], cscv: CscvConfig) -> Result<PboOutcome, ReportError> {
    let mut columns = Vec::new();
    let mut dates: Option<(&str, Vec<NaiveDate>)> = None;
    for t in completed {
        let metrics = t.metrics().expect("completed");
        let Some(returns) = metrics.daily_returns() else {
            continue; // ruined: no return series past the ruin
        };
        let these: Vec<NaiveDate> = metrics.equity.iter().map(|p| p.date).collect();
        match &dates {
            None => dates = Some((t.trial_id.as_str(), these)),
            Some((first, d)) if *d != these => {
                return Err(ReportError::MisalignedDates {
                    first: (*first).to_owned(),
                    trial_id: t.trial_id.clone(),
                });
            }
            Some(_) => {}
        }
        columns.push(returns);
    }
    let pbo = match pbo_cscv(&columns, cscv) {
        Err(e) => PboOutcome::NotComputed {
            reason: e.to_string(),
        },
        Ok(r) => match (r.pbo, LogitSummary::of(&r.logits)) {
            (Some(pbo), Some(logits)) => PboOutcome::Computed(PboReport {
                cscv: r.config,
                trials: columns.len(),
                observations: columns[0].len(),
                combinations: r.combinations,
                pbo,
                logits,
            }),
            _ => PboOutcome::NotComputed {
                reason: "every partition was skipped (no trial has a defined Sharpe)".to_owned(),
            },
        },
    };

    Ok(pbo)
}
