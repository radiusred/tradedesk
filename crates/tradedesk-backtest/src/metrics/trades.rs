//! Statistics over a run's closed trades. Positions still open at the end of the run are
//! not trades here; [`super::OpenAtEnd`] reports them.

use serde::{Deserialize, Serialize};

use crate::{ClosedTrade, ExitReason};

/// Costs in the account currency, split by component.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct CostBreakdown {
    /// Venue half-spread on every fill.
    pub spread: f64,
    /// Slippage on every fill.
    pub slippage: f64,
    /// Commission, including the per-round-trip charge.
    pub commission: f64,
    /// Overnight financing (negative is a credit).
    pub financing: f64,
}

impl CostBreakdown {
    /// The sum of the four components.
    #[must_use]
    pub fn total(&self) -> f64 {
        self.spread + self.slippage + self.commission + self.financing
    }
}

/// Count and net P&L of the trades with one exit reason.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct ReasonStats {
    /// Trades.
    pub count: usize,
    /// Their net P&L, account currency.
    pub net_pnl: f64,
}

/// Closed trades by exit reason.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
pub struct ExitReasonBreakdown {
    /// The strategy's exit signal.
    pub signal: ReasonStats,
    /// The stop level.
    pub stop: ReasonStats,
    /// The target level.
    pub target: ReasonStats,
    /// Closed by the engine at the end of the run.
    pub end_of_run: ReasonStats,
}

impl ExitReasonBreakdown {
    fn entry(&mut self, reason: ExitReason) -> &mut ReasonStats {
        match reason {
            ExitReason::Signal => &mut self.signal,
            ExitReason::Stop => &mut self.stop,
            ExitReason::Target => &mut self.target,
            ExitReason::EndOfRun => &mut self.end_of_run,
        }
    }
}

/// Trade-level statistics. Money is net of every cost and in the account currency unless
/// a field says gross. A figure with no trades to average over is `None`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TradeStats {
    /// Closed trades.
    pub count: usize,
    /// Trades with net P&L above zero.
    pub wins: usize,
    /// Trades with net P&L below zero.
    pub losses: usize,
    /// Trades with net P&L of exactly zero.
    pub breakevens: usize,
    /// `wins / count`.
    pub win_rate: Option<f64>,
    /// Mean net P&L of the winning trades.
    pub average_win: Option<f64>,
    /// Mean net P&L of the losing trades (negative).
    pub average_loss: Option<f64>,
    /// Mean net P&L per trade.
    pub expectancy: Option<f64>,
    /// Mean gross P&L per trade (before every cost), to set against `expectancy`.
    pub gross_expectancy: Option<f64>,
    /// Sum of wins over the absolute sum of losses. `None` when no trade lost (the ratio
    /// is unbounded); `0` when trades lost and none won.
    pub profit_factor: Option<f64>,
    /// Mean bars held on the engine's stepping series.
    pub average_bars_held: Option<f64>,
    /// Most bars held.
    pub max_bars_held: Option<usize>,
    /// Mean calendar days held, `(exit_ts − entry_ts) / 24h`.
    pub average_days_held: Option<f64>,
    /// Most calendar days held.
    pub max_days_held: Option<f64>,
    /// Longest run of consecutive winning trades, in closing order.
    pub longest_winning_streak: usize,
    /// Longest run of consecutive losing trades, in closing order.
    pub longest_losing_streak: usize,
    /// Total gross P&L.
    pub gross_pnl: f64,
    /// Total net P&L.
    pub net_pnl: f64,
    /// `gross_pnl − net_pnl` by component: the cost drag.
    pub costs: CostBreakdown,
    /// Count and net P&L per exit reason.
    pub by_exit_reason: ExitReasonBreakdown,
}

#[allow(clippy::cast_precision_loss)]
fn mean(sum: f64, n: usize) -> Option<f64> {
    (n > 0).then(|| sum / n as f64)
}

/// Statistics over `trades`, in closing order.
#[must_use]
#[allow(clippy::cast_precision_loss)]
pub fn trade_stats(trades: &[ClosedTrade]) -> TradeStats {
    let mut wins = (0, 0.0);
    let mut losses = (0, 0.0);
    let mut breakevens = 0;
    let mut streak = (0usize, 0usize);
    let mut longest = (0usize, 0usize);
    let mut gross = 0.0;
    let mut net = 0.0;
    let mut costs = CostBreakdown::default();
    let mut by_reason = ExitReasonBreakdown::default();
    let mut bars = 0usize;
    let mut max_bars: Option<usize> = None;
    let mut days = 0.0;
    let mut max_days: Option<f64> = None;
    for t in trades {
        let pnl = t.net_pnl_account;
        if pnl > 0.0 {
            wins = (wins.0 + 1, wins.1 + pnl);
            streak = (streak.0 + 1, 0);
        } else if pnl < 0.0 {
            losses = (losses.0 + 1, losses.1 + pnl);
            streak = (0, streak.1 + 1);
        } else {
            breakevens += 1;
            streak = (0, 0);
        }
        longest = (longest.0.max(streak.0), longest.1.max(streak.1));
        gross += t.gross_pnl_account;
        net += pnl;
        costs.spread += t.spread_cost * t.fx_rate;
        costs.slippage += t.slippage_cost * t.fx_rate;
        costs.commission += t.commission;
        costs.financing += t.financing * t.fx_rate;
        let r = by_reason.entry(t.exit_reason);
        r.count += 1;
        r.net_pnl += pnl;
        bars += t.bars_held;
        max_bars = Some(max_bars.map_or(t.bars_held, |m| m.max(t.bars_held)));
        let held = (t.exit_ts - t.entry_ts).num_milliseconds() as f64 / 86_400_000.0;
        days += held;
        max_days = Some(max_days.map_or(held, |m: f64| m.max(held)));
    }
    let n = trades.len();
    TradeStats {
        count: n,
        wins: wins.0,
        losses: losses.0,
        breakevens,
        win_rate: mean(wins.0 as f64, n),
        average_win: mean(wins.1, wins.0),
        average_loss: mean(losses.1, losses.0),
        expectancy: mean(net, n),
        gross_expectancy: mean(gross, n),
        profit_factor: (losses.0 > 0).then(|| wins.1 / losses.1.abs()),
        average_bars_held: mean(bars as f64, n),
        max_bars_held: max_bars,
        average_days_held: mean(days, n),
        max_days_held: max_days,
        longest_winning_streak: longest.0,
        longest_losing_streak: longest.1,
        gross_pnl: gross,
        net_pnl: net,
        costs,
        by_exit_reason: by_reason,
    }
}
