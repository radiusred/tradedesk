//! Run metrics (M1-R5): the daily marked-to-market equity curve, the annualised Sharpe
//! from daily returns, the maximum drawdown in the account currency and in percent, and
//! trade statistics, computed from a finished [`Ledger`].
//!
//! Conventions (the crate README's "Metrics" section has the full definitions):
//! - **Day.** A UTC weekday, Monday to Friday, marked at its close (the next `00:00Z`).
//!   Saturdays and Sundays are omitted from the curve and from every statistic; their
//!   P&L lands in Monday's point. A window whose last day is a Saturday or Sunday has no
//!   Monday to land in, so it ends with one more point, a [`PointKind::WeekendClose`]
//!   marked at the window's close. A weekday with no bars carries the last mark forward.
//!   The curve's last point is therefore always the window's close, and the final
//!   equity reconciles with the closed trades and [`OpenAtEnd`].
//! - **Mark.** Liquidation value: open positions are valued as if closed at the
//!   configured-side close, with the exit half-spread, slippage and commission deducted.
//! - **Sharpe.** `sqrt(252) × mean(r − rf/252) / std(r)`, sample std (ddof 1), over the
//!   daily simple returns of the curve, reported with the number of observations.
//!
//! Everything is a pure function of the run: [`Metrics::compute`] clones the ledger,
//! accrues financing to the window's close on the clone and rebuilds each day from the
//! ledger's records.

mod drawdown;
mod equity;
mod returns;
mod trades;

use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};

pub use drawdown::{Drawdown, MaxDrawdown, max_drawdown};
pub use equity::{EquityPoint, OpenPositionValue, PointKind};
pub use returns::{PERIODS_PER_YEAR, ReturnStats, STD_DDOF, return_stats};
pub use trades::{CostBreakdown, ExitReasonBreakdown, ReasonStats, TradeStats, trade_stats};

use crate::{Ledger, LedgerError, MarkSource, PositionId, RunMetadata};

/// Why metrics could not be computed.
#[derive(Debug, Clone, PartialEq, thiserror::Error)]
pub enum MetricsError {
    /// The starting capital is not finite and positive.
    #[error("starting capital {0} must be finite and greater than zero")]
    InvalidCapital(f64),
    /// The risk-free rate is not finite.
    #[error("risk-free rate {0} must be finite")]
    InvalidRiskFreeRate(f64),
    /// The window's last day is before its first.
    #[error("window {first}..={last} is empty")]
    EmptyWindow {
        /// First day.
        first: NaiveDate,
        /// Last day.
        last: NaiveDate,
    },
    /// The window holds no Monday-to-Friday day to mark.
    #[error("window {first}..={last} holds no weekday")]
    NoTradingDay {
        /// First day.
        first: NaiveDate,
        /// Last day.
        last: NaiveDate,
    },
    /// A fill falls outside the window, so the curve would not account for it.
    #[error("fill for position {position:?} at {ts} is outside the window {start}..={end}")]
    OutsideWindow {
        /// Position.
        position: PositionId,
        /// Fill timestamp.
        ts: DateTime<Utc>,
        /// Window open (`first` at `00:00Z`).
        start: DateTime<Utc>,
        /// Window close (`last + 1` at `00:00Z`).
        end: DateTime<Utc>,
    },
    /// A closed trade has no entry fill in the ledger.
    #[error("position {0:?} has no entry fill")]
    MissingEntryFill(PositionId),
    /// A mark or a liquidation price could not be had (no bar, divergent bar, no FX rate).
    #[error(transparent)]
    Ledger(#[from] LedgerError),
}

/// What a metrics computation needs beyond the ledger.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct MetricsConfig {
    /// First UTC day of the run window.
    pub first_day: NaiveDate,
    /// Last UTC day of the run window (inclusive).
    pub last_day: NaiveDate,
    /// Equity before the first trade, in the account currency (finite, above zero).
    pub starting_capital: f64,
    /// Annual risk-free rate, as a fraction (0.04 is 4%). Defaults to 0.
    pub risk_free_rate: f64,
}

impl MetricsConfig {
    /// A window from `first_day` to `last_day` inclusive (as in `LoadRequest::utc_days`)
    /// with a zero risk-free rate.
    #[must_use]
    pub fn new(first_day: NaiveDate, last_day: NaiveDate, starting_capital: f64) -> Self {
        Self {
            first_day,
            last_day,
            starting_capital,
            risk_free_rate: 0.0,
        }
    }

    fn validate(&self) -> Result<Vec<(NaiveDate, PointKind)>, MetricsError> {
        if !self.starting_capital.is_finite() || self.starting_capital <= 0.0 {
            return Err(MetricsError::InvalidCapital(self.starting_capital));
        }
        if !self.risk_free_rate.is_finite() {
            return Err(MetricsError::InvalidRiskFreeRate(self.risk_free_rate));
        }
        let (first, last) = (self.first_day, self.last_day);
        if last < first {
            return Err(MetricsError::EmptyWindow { first, last });
        }
        let days = equity::mark_days(first, last);
        if days.is_empty() {
            return Err(MetricsError::NoTradingDay { first, last });
        }
        Ok(days)
    }
}

/// How a day is defined.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DayConvention {
    /// UTC Monday to Friday, marked at the next `00:00Z`; weekends omitted and their
    /// P&L carried by the next weekday. A window ending on a Saturday or Sunday ends
    /// with a [`PointKind::WeekendClose`] at the window's close.
    UtcWeekdayClose,
}

/// How an open position is valued.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum MarkConvention {
    /// As if closed at the configured-side close, net of exit spread, slippage and
    /// commission.
    Liquidation,
}

/// The conventions behind every figure, written out with the results.
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Conventions {
    /// Day definition.
    pub day: DayConvention,
    /// Mark definition.
    pub mark: MarkConvention,
    /// Annualisation periods (252).
    pub periods_per_year: f64,
    /// ddof of every standard deviation (1, the sample std).
    pub std_ddof: u32,
    /// Annual risk-free rate subtracted (as `rf / 252` a day) in Sharpe and Sortino.
    pub risk_free_rate: f64,
}

/// Positions still open when the window closes. They are not closed trades and are not
/// in [`TradeStats`]; their value is in the last equity point.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct OpenAtEnd {
    /// Open positions.
    pub count: usize,
    /// Their liquidation value, account currency.
    pub unrealised_net: f64,
    /// Exit costs deducted in `unrealised_net`.
    pub exit_cost: f64,
    /// Each position, valued at the window's close.
    pub positions: Vec<OpenPositionValue>,
}

/// Everything M1-R5 reports for one run. Money is f64 in `account_currency`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Metrics {
    /// The run's venue, product, exit mode, FX table and cost placeholders.
    pub run: RunMetadata,
    /// The currency of every money figure here.
    pub account_currency: String,
    /// Day, mark and Sharpe conventions.
    pub conventions: Conventions,
    /// First UTC day of the window.
    pub first_day: NaiveDate,
    /// Last UTC day of the window (inclusive).
    pub last_day: NaiveDate,
    /// Equity before the first trade.
    pub starting_capital: f64,
    /// Equity at the last close.
    pub final_equity: f64,
    /// `final_equity / starting_capital − 1`.
    pub total_return: f64,
    /// The first day equity was zero or below. Return statistics are undefined after it.
    pub ruined_on: Option<NaiveDate>,
    /// Sharpe and the other statistics of the daily returns.
    pub returns: ReturnStats,
    /// The largest peak-to-trough losses, in the account currency and in percent.
    pub max_drawdown: MaxDrawdown,
    /// Statistics over the closed trades.
    pub trades: TradeStats,
    /// Positions open at the window's close.
    pub open_at_end: OpenAtEnd,
    /// One point per UTC weekday in the window, then a weekend close when the window
    /// ends on a Saturday or Sunday. The last point is the window's close.
    pub equity: Vec<EquityPoint>,
}

impl Metrics {
    /// Compute every metric for the run booked in `ledger`, marking from `marks`.
    ///
    /// The caller's ledger is not changed: a clone has its financing accrued to the
    /// window's close before anything is marked.
    ///
    /// # Errors
    /// [`MetricsError`] for an invalid config, a fill outside the window, or a position
    /// that cannot be marked at a close (no bar, a divergent bar, no FX rate).
    pub fn compute(
        ledger: &Ledger,
        marks: &impl MarkSource,
        config: &MetricsConfig,
    ) -> Result<Self, MetricsError> {
        let days = config.validate()?;
        let start = equity::day_start(config.first_day);
        let end = equity::day_start(config.last_day + chrono::Days::new(1));
        if let Some(f) = ledger.fills().iter().find(|f| f.ts < start || f.ts > end) {
            return Err(MetricsError::OutsideWindow {
                position: f.position,
                ts: f.ts,
                start,
                end,
            });
        }
        let mut ledger = ledger.clone();
        ledger.accrue_financing(end, marks)?;

        let curve = equity::build_curve(&ledger, marks, &days, config.starting_capital)?;
        let open = equity::open_at(&ledger, end, marks)?;
        let final_equity = curve.last().map_or(config.starting_capital, |p| p.equity);
        let ruined_on = curve.iter().find(|p| p.equity <= 0.0).map(|p| p.date);
        let returns = if ruined_on.is_some() {
            ReturnStats::undefined(curve.len())
        } else {
            let r: Vec<f64> = curve.iter().filter_map(|p| p.simple_return).collect();
            return_stats(&r, config.risk_free_rate)
        };
        let opening = config.first_day.pred_opt().unwrap_or(config.first_day);
        let dated: Vec<(NaiveDate, f64)> = std::iter::once((opening, config.starting_capital))
            .chain(curve.iter().map(|p| (p.date, p.equity)))
            .collect();
        let metadata = ledger.metadata().clone();
        Ok(Self {
            account_currency: metadata.account_fx.account_currency().to_owned(),
            run: metadata,
            conventions: Conventions {
                day: DayConvention::UtcWeekdayClose,
                mark: MarkConvention::Liquidation,
                periods_per_year: PERIODS_PER_YEAR,
                std_ddof: STD_DDOF,
                risk_free_rate: config.risk_free_rate,
            },
            first_day: config.first_day,
            last_day: config.last_day,
            starting_capital: config.starting_capital,
            final_equity,
            total_return: final_equity / config.starting_capital - 1.0,
            ruined_on,
            returns,
            max_drawdown: max_drawdown(&dated),
            trades: trade_stats(ledger.closed_trades()),
            open_at_end: OpenAtEnd {
                count: open.len(),
                unrealised_net: open.iter().map(|v| v.unrealised_net).sum(),
                exit_cost: open.iter().map(|v| v.exit_cost).sum(),
                positions: open,
            },
            equity: curve,
        })
    }

    /// The daily simple returns behind the Sharpe, one per equity point; `None` once the
    /// run is ruined. #33's registry stores these per trial.
    #[must_use]
    pub fn daily_returns(&self) -> Option<Vec<f64>> {
        if self.ruined_on.is_some() {
            return None;
        }
        self.equity.iter().map(|p| p.simple_return).collect()
    }
}

#[cfg(test)]
mod tests;
