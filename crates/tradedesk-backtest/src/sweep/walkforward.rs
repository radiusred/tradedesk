//! The rolling walk-forward over one sweep's registry records.
//!
//! It is analysis over the registry: every trial is one continuous run over the whole
//! window, and the walk-forward only chooses which trial's days to read.
//!
//! - **Folds.** From the window's first day, a `train_months` train window followed by a
//!   `test_months` test window, stepped by `step_months`, while the test window starts
//!   on or before the window's last day. The last test window is clipped at the last
//!   day. The defaults are 24 / 6 / 6: over a window of six years and two months that
//!   is nine folds, the ninth tested on the last two months.
//! - **Days.** A fold's train and test days are the trial's equity points dated inside
//!   those calendar windows (UTC weekdays, plus a weekend close; the crate README's
//!   "Day convention"). Every trial of a sweep has points on the same dates; that is
//!   checked.
//! - **Selection.** In each fold, the completed trial that is not ruined with the highest
//!   annualised Sharpe of its daily simple returns on the train days. A tie goes to the
//!   lower cell index, and an undefined Sharpe (fewer than two days, no variance) ranks
//!   last. A trial ruined anywhere in the window is never selected, as in the PBO matrix.
//! - **Stitched out-of-sample series.** The selected trial's daily returns on its fold's
//!   test days, fold after fold. Its Sharpe is `√252 × mean / std` ([`return_stats`]).
//!   The £ curve starts at the starting capital and adds the selected trial's daily £
//!   equity change (`equity − previous equity`) on each test day; its maximum drawdown
//!   is [`max_drawdown`]'s.
//! - **Out-of-sample trades.** A selected trial's closed trades that the equity curve
//!   books inside its test window: `exit_ts` after the mark of the last point before the
//!   window and at or before the mark of the window's last point. A trade opened in the
//!   train window and closed in the test window counts, with all of its costs. The cost
//!   drag is the trades' total cost over their gross P&L, defined when that gross is
//!   above zero.
//! - **Each cell over the same days.** Every completed trial's own fixed-parameter
//!   Sharpe over the whole out-of-sample span, with its gross and net £ P&L there.

use std::collections::BTreeMap;

use chrono::{DateTime, Months, NaiveDate, Utc};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use super::registry::{RegistryRecord, TrialRecord};
use super::report::{ReportError, sweep_trials};
use crate::ledger::ClosedTrade;
use crate::metrics::{
    EquityPoint, MaxDrawdown, ReturnStats, TradeStats, max_drawdown, return_stats, trade_stats,
};

/// The walk-forward's window lengths, in calendar months.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WalkForwardOptions {
    /// Train window (24).
    pub train_months: u32,
    /// Test window (6).
    pub test_months: u32,
    /// Step between folds (6). At least `test_months`, so test windows never overlap.
    pub step_months: u32,
}

impl Default for WalkForwardOptions {
    /// The default convention: 24-month train, 6-month test, 6-month step.
    fn default() -> Self {
        Self {
            train_months: 24,
            test_months: 6,
            step_months: 6,
        }
    }
}

/// Why a walk-forward could not be computed.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum WalkForwardError {
    /// The sweep could not be identified, or its trials' dates differ.
    #[error(transparent)]
    Sweep(#[from] ReportError),
    /// A window length is zero, or the step is shorter than the test window.
    #[error(
        "walk-forward months must be at least 1, with the step at least the test window \
         (train {train}, test {test}, step {step})"
    )]
    InvalidOptions {
        /// Train months.
        train: u32,
        /// Test months.
        test: u32,
        /// Step months.
        step: u32,
    },
    /// No trial of the sweep completed.
    #[error("the sweep has no completed trial")]
    NoCompletedTrial,
    /// The window is too short for one train window and a test day.
    #[error("the window {first}..={last} holds no fold with a test day")]
    NoFold {
        /// First day of the window.
        first: NaiveDate,
        /// Last day of the window.
        last: NaiveDate,
    },
}

/// One fold's calendar windows, inclusive.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fold {
    /// 1-based fold number.
    pub fold: usize,
    /// First train day.
    pub train_first: NaiveDate,
    /// Last train day.
    pub train_last: NaiveDate,
    /// First test day.
    pub test_first: NaiveDate,
    /// Last test day (the window's last day for a short final fold).
    pub test_last: NaiveDate,
}

/// The folds of a window `first..=last`: train windows start every `step_months` from
/// `first`, and a fold exists while its test window starts on or before `last`. Empty
/// when an option is zero.
#[must_use]
pub fn folds(first: NaiveDate, last: NaiveDate, options: WalkForwardOptions) -> Vec<Fold> {
    let WalkForwardOptions {
        train_months,
        test_months,
        step_months,
    } = options;
    let mut out = Vec::new();
    if train_months == 0 || test_months == 0 || step_months == 0 {
        return out;
    }
    for k in 0_u32.. {
        let Some(train_first) = k
            .checked_mul(step_months)
            .and_then(|m| first.checked_add_months(Months::new(m)))
        else {
            break;
        };
        let Some(test_first) = train_first.checked_add_months(Months::new(train_months)) else {
            break;
        };
        if test_first > last {
            break;
        }
        let test_last = test_first
            .checked_add_months(Months::new(test_months))
            .and_then(|d| d.pred_opt())
            .map_or(last, |d| d.min(last));
        out.push(Fold {
            fold: out.len() + 1,
            train_first,
            train_last: test_first.pred_opt().unwrap_or(test_first),
            test_first,
            test_last,
        });
    }
    out
}

/// The trial a fold selected, and how it did.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FoldSelection {
    /// Cell index.
    pub index: usize,
    /// Trial id.
    pub trial_id: String,
    /// The cell's parameters.
    pub params: BTreeMap<String, Value>,
    /// In-sample: the annualised Sharpe on the train days; `None` when undefined.
    pub train_sharpe: Option<f64>,
    /// Out-of-sample: the annualised Sharpe on the test days; `None` when undefined.
    pub test_sharpe: Option<f64>,
    /// The £ equity change over the test days.
    pub test_pnl: f64,
    /// Closed trades booked in the test window; `None` when the record has no trades.
    pub test_trades: Option<usize>,
}

/// One fold's windows, day counts and selection.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct FoldResult {
    /// The calendar windows.
    pub fold: Fold,
    /// Equity points in the train window.
    pub train_days: usize,
    /// Equity points in the test window.
    pub test_days: usize,
    /// The selected trial; `None` when no completed trial is free of ruin.
    pub selected: Option<FoldSelection>,
}

/// One day of the stitched out-of-sample series.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct StitchedDay {
    /// The equity point's date.
    pub date: NaiveDate,
    /// The fold it is tested in.
    pub fold: usize,
    /// The selected trial's cell index.
    pub index: usize,
    /// The selected trial's daily simple return.
    pub simple_return: f64,
    /// The selected trial's £ equity change that day.
    pub pnl: f64,
    /// The stitched £ curve after the day.
    pub equity: f64,
}

/// The out-of-sample closed trades of the selected trials.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OosTrades {
    /// Statistics over them, in closing order: the count, gross and net P&L and the cost
    /// by component.
    pub stats: TradeStats,
    /// `costs.total() / gross_pnl`; `None` unless the gross P&L is above zero.
    pub cost_drag: Option<f64>,
}

/// The stitched out-of-sample series.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Stitched {
    /// First out-of-sample day.
    pub first_day: NaiveDate,
    /// Last out-of-sample day.
    pub last_day: NaiveDate,
    /// Statistics of the stitched daily returns; `returns.sharpe` is the stitched OOS
    /// Sharpe (√252).
    pub returns: ReturnStats,
    /// The £ curve's opening equity (the trials' starting capital).
    pub starting_capital: f64,
    /// The £ curve's last value.
    pub final_equity: f64,
    /// `final_equity − starting_capital`.
    pub pnl: f64,
    /// The £ curve's maximum drawdowns, opening at the starting capital the day before
    /// the first out-of-sample day.
    pub max_drawdown: MaxDrawdown,
    /// The selected trials' out-of-sample trades; `None` when a selected record has no
    /// trades.
    pub trades: Option<OosTrades>,
    /// Every stitched day.
    pub days: Vec<StitchedDay>,
}

/// A cell's own fixed-parameter result over every out-of-sample day.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CellOos {
    /// Cell index.
    pub index: usize,
    /// Trial id.
    pub trial_id: String,
    /// The cell's parameters.
    pub params: BTreeMap<String, Value>,
    /// Whether the run was ruined (its Sharpe is then undefined).
    pub ruined: bool,
    /// Out-of-sample days.
    pub observations: usize,
    /// Annualised Sharpe over them; `None` when undefined.
    pub sharpe: Option<f64>,
    /// Gross £ P&L over them (realised and unrealised, before every cost).
    pub gross_pnl: f64,
    /// Net £ P&L over them: the equity change.
    pub net_pnl: f64,
    /// Closed trades booked over them; `None` when the record has no trades.
    pub trades: Option<usize>,
}

/// The walk-forward over one sweep.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WalkForwardReport {
    /// The sweep.
    pub sweep_id: String,
    /// The window lengths.
    pub options: WalkForwardOptions,
    /// First day of the sweep's window.
    pub first_day: NaiveDate,
    /// Last day of the sweep's window.
    pub last_day: NaiveDate,
    /// Distinct trials recorded.
    pub trials: usize,
    /// Trials that completed.
    pub completed: usize,
    /// Cells recorded as failures.
    pub failed: usize,
    /// Completed trials that were ruined.
    pub ruined: usize,
    /// Whether the sweep may be reported as costed: every completed trial is costed.
    pub costed: bool,
    /// Every fold.
    pub folds: Vec<FoldResult>,
    /// Mean of the selected trials' defined train Sharpes.
    pub mean_is_sharpe: Option<f64>,
    /// Mean of the selected trials' defined test Sharpes.
    pub mean_oos_sharpe: Option<f64>,
    /// The stitched series; `None` when a fold selected no trial.
    pub stitched: Option<Stitched>,
    /// Every completed trial over the out-of-sample days, in cell order.
    pub cells: Vec<CellOos>,
}

/// What the walk-forward reads from one completed trial.
#[derive(Debug, Clone, Copy)]
struct View<'a> {
    index: usize,
    trial_id: &'a str,
    params: &'a BTreeMap<String, Value>,
    ruined: bool,
    costed: bool,
    capital: f64,
    risk_free_rate: f64,
    points: &'a [EquityPoint],
    trades: Option<&'a [ClosedTrade]>,
}

impl<'a> View<'a> {
    fn of(t: &'a TrialRecord) -> Option<Self> {
        let m = t.metrics()?;
        Some(Self {
            index: t.index,
            trial_id: &t.trial_id,
            params: &t.params,
            ruined: m.ruined_on.is_some(),
            costed: t.run.is_costed(&t.instrument),
            capital: t.starting_capital,
            risk_free_rate: t.risk_free_rate,
            points: &m.equity,
            trades: t.trades(),
        })
    }

    /// Return statistics over the points in `range`; undefined for a ruined run.
    fn stats(&self, range: std::ops::Range<usize>) -> ReturnStats {
        let returns: Option<Vec<f64>> = self.points[range.clone()]
            .iter()
            .map(|p| p.simple_return)
            .collect();
        match returns {
            Some(r) if !self.ruined => return_stats(&r, self.risk_free_rate),
            _ => ReturnStats::undefined(range.len()),
        }
    }

    /// The equity before point `i`.
    fn equity_before(&self, i: usize) -> f64 {
        if i == 0 {
            self.capital
        } else {
            self.points[i - 1].equity
        }
    }

    /// The trades the curve books in the points `range`.
    fn trades_in(&self, range: std::ops::Range<usize>) -> Option<Vec<&'a ClosedTrade>> {
        let after: Option<DateTime<Utc>> = range.start.checked_sub(1).map(|i| self.points[i].at);
        let until = self.points[range.end - 1].at;
        let trades = self.trades?;
        Some(
            trades
                .iter()
                .filter(|t| t.exit_ts <= until && after.is_none_or(|a| t.exit_ts > a))
                .collect(),
        )
    }
}

/// The point indices dated `first..=last` in `dates` (ascending).
fn day_range(dates: &[NaiveDate], first: NaiveDate, last: NaiveDate) -> std::ops::Range<usize> {
    dates.partition_point(|d| *d < first)..dates.partition_point(|d| *d <= last)
}

#[allow(clippy::cast_precision_loss)]
fn mean(xs: &[f64]) -> Option<f64> {
    (!xs.is_empty()).then(|| xs.iter().sum::<f64>() / xs.len() as f64)
}

impl WalkForwardReport {
    /// The walk-forward over `sweep_id` (or the only sweep in `records`).
    ///
    /// # Errors
    /// [`WalkForwardError`] when the sweep cannot be identified, its trials' dates
    /// differ, no trial completed, the options are invalid or the window holds no fold.
    pub fn from_records(
        records: &[RegistryRecord],
        sweep_id: Option<&str>,
        options: WalkForwardOptions,
    ) -> Result<Self, WalkForwardError> {
        let (sweep_id, trials) = sweep_trials(records, sweep_id)?;
        let views: Vec<View<'_>> = trials.iter().filter_map(|t| View::of(t)).collect();
        let window = trials
            .iter()
            .find(|t| t.metrics().is_some())
            .map(|t| (t.first_day, t.last_day))
            .ok_or(WalkForwardError::NoCompletedTrial)?;
        Self::compute(sweep_id, trials.len(), window, &views, options)
    }

    #[allow(clippy::too_many_lines)]
    fn compute(
        sweep_id: String,
        trials: usize,
        (first_day, last_day): (NaiveDate, NaiveDate),
        views: &[View<'_>],
        options: WalkForwardOptions,
    ) -> Result<Self, WalkForwardError> {
        let WalkForwardOptions {
            train_months: train,
            test_months: test,
            step_months: step,
        } = options;
        if train == 0 || test == 0 || step < test {
            return Err(WalkForwardError::InvalidOptions { train, test, step });
        }
        let first = views.first().ok_or(WalkForwardError::NoCompletedTrial)?;
        let dates: Vec<NaiveDate> = first.points.iter().map(|p| p.date).collect();
        for v in views {
            if !v.points.iter().map(|p| p.date).eq(dates.iter().copied()) {
                return Err(ReportError::MisalignedDates {
                    first: first.trial_id.to_owned(),
                    trial_id: v.trial_id.to_owned(),
                }
                .into());
            }
        }
        let calendar: Vec<(Fold, std::ops::Range<usize>, std::ops::Range<usize>)> =
            folds(first_day, last_day, options)
                .into_iter()
                .map(|f| {
                    let train = day_range(&dates, f.train_first, f.train_last);
                    let test = day_range(&dates, f.test_first, f.test_last);
                    (f, train, test)
                })
                .filter(|(_, _, test)| !test.is_empty())
                .collect();
        if calendar.is_empty() {
            return Err(WalkForwardError::NoFold {
                first: first_day,
                last: last_day,
            });
        }

        let mut fold_results = Vec::with_capacity(calendar.len());
        let mut chosen: Vec<Option<&View<'_>>> = Vec::with_capacity(calendar.len());
        for (fold, train_range, test_range) in &calendar {
            // Cell order; a later trial replaces the best only with a strictly higher
            // Sharpe, so a tie keeps the lower index and an undefined one ranks last.
            let mut best: Option<(&View<'_>, Option<f64>)> = None;
            for v in views.iter().filter(|v| !v.ruined) {
                let sharpe = v.stats(train_range.clone()).sharpe;
                let better = match (best, sharpe) {
                    (None, _) | (Some((_, None)), Some(_)) => true,
                    (Some((_, Some(b))), Some(s)) => s > b,
                    (Some(_), None) => false,
                };
                if better {
                    best = Some((v, sharpe));
                }
            }
            chosen.push(best.map(|(v, _)| v));
            let selected = best.map(|(v, train_sharpe)| FoldSelection {
                index: v.index,
                trial_id: v.trial_id.to_owned(),
                params: v.params.clone(),
                train_sharpe,
                test_sharpe: v.stats(test_range.clone()).sharpe,
                test_pnl: v.points[test_range.end - 1].equity - v.equity_before(test_range.start),
                test_trades: v.trades_in(test_range.clone()).map(|t| t.len()),
            });
            fold_results.push(FoldResult {
                fold: *fold,
                train_days: train_range.len(),
                test_days: test_range.len(),
                selected,
            });
        }
        let is: Vec<f64> = fold_results
            .iter()
            .filter_map(|f| f.selected.as_ref()?.train_sharpe)
            .collect();
        let oos: Vec<f64> = fold_results
            .iter()
            .filter_map(|f| f.selected.as_ref()?.test_sharpe)
            .collect();

        let stitched = chosen
            .iter()
            .copied()
            .collect::<Option<Vec<&View<'_>>>>()
            .map(|chosen| stitch(&calendar, &chosen));

        let oos_span = calendar[0].2.start..calendar[calendar.len() - 1].2.end;
        let cells = views
            .iter()
            .map(|v| {
                let start_gross = oos_span
                    .start
                    .checked_sub(1)
                    .map_or(0.0, |i| v.points[i].gross_pnl);
                let end = &v.points[oos_span.end - 1];
                CellOos {
                    index: v.index,
                    trial_id: v.trial_id.to_owned(),
                    params: v.params.clone(),
                    ruined: v.ruined,
                    observations: oos_span.len(),
                    sharpe: v.stats(oos_span.clone()).sharpe,
                    gross_pnl: end.gross_pnl - start_gross,
                    net_pnl: end.equity - v.equity_before(oos_span.start),
                    trades: v.trades_in(oos_span.clone()).map(|t| t.len()),
                }
            })
            .collect();

        Ok(Self {
            sweep_id,
            options,
            first_day,
            last_day,
            trials,
            completed: views.len(),
            failed: trials - views.len(),
            ruined: views.iter().filter(|v| v.ruined).count(),
            costed: views.iter().all(|v| v.costed),
            folds: fold_results,
            mean_is_sharpe: mean(&is),
            mean_oos_sharpe: mean(&oos),
            stitched,
            cells,
        })
    }
}

/// Stitch the chosen trial of every fold over its test days.
fn stitch(
    calendar: &[(Fold, std::ops::Range<usize>, std::ops::Range<usize>)],
    chosen: &[&View<'_>],
) -> Stitched {
    let capital = chosen[0].capital;
    let mut equity = capital;
    let mut days = Vec::new();
    let mut returns = Vec::new();
    let mut trades: Option<Vec<ClosedTrade>> = Some(Vec::new());
    for ((fold, _, test), v) in calendar.iter().zip(chosen) {
        for i in test.clone() {
            let p = &v.points[i];
            // A selected trial is never ruined, so every return is defined.
            let r = p.simple_return.unwrap_or(f64::NAN);
            let pnl = p.equity - v.equity_before(i);
            equity += pnl;
            returns.push(r);
            days.push(StitchedDay {
                date: p.date,
                fold: fold.fold,
                index: v.index,
                simple_return: r,
                pnl,
                equity,
            });
        }
        trades = match (trades, v.trades_in(test.clone())) {
            (Some(mut all), Some(these)) => {
                all.extend(these.into_iter().cloned());
                Some(all)
            }
            _ => None,
        };
    }
    let first_day = days[0].date;
    let opening = first_day.pred_opt().unwrap_or(first_day);
    let dated: Vec<(NaiveDate, f64)> = std::iter::once((opening, capital))
        .chain(days.iter().map(|d| (d.date, d.equity)))
        .collect();
    let trades = trades.map(|mut t| {
        t.sort_by_key(|t| t.exit_ts);
        let stats = trade_stats(&t);
        let cost_drag = (stats.gross_pnl > 0.0).then(|| stats.costs.total() / stats.gross_pnl);
        OosTrades { stats, cost_drag }
    });
    Stitched {
        first_day,
        last_day: days[days.len() - 1].date,
        returns: return_stats(&returns, chosen[0].risk_free_rate),
        starting_capital: capital,
        final_equity: equity,
        pnl: equity - capital,
        max_drawdown: max_drawdown(&dated),
        trades,
        days,
    }
}

#[cfg(test)]
mod tests;
