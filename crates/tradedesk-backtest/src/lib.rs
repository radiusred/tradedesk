//! The tradedesk backtester (research only; no live trading).
//!
//! The data layer turns the per-side 1-minute streams a [`tradedesk_data::Reader`]
//! yields into joined bid/ask series, passes 1-minute bars through and aggregates them to
//! 5m / 15m / 1h / 1d, and loads each instrument's window into memory once per run so
//! parallel sweep cells can borrow it.
//!
//! The execution layer prices fills under an explicit venue cost model ([`cost`]),
//! charges overnight financing on the venue's rollover calendar, evaluates stops and
//! targets under a per-run [`ExitEvaluation`], and books everything in a [`Ledger`].
//!
//! [`metrics`] turns a finished ledger into the daily marked-to-market equity curve, the
//! annualised Sharpe from daily returns, the maximum drawdown in pounds and percent, and
//! trade statistics ([`Metrics`]).
//!
//! On top of the data layer, [`indicators`] holds the streaming technical indicators
//! and [`strategy`] the [`strategy::Strategy`] trait and its signal types. Strategies
//! emit signals; they never fill orders. **No strategy ships in this crate**: a strategy
//! crate implements the trait and registers a factory for it in a
//! [`sweep::StrategyRegistry`].
//!
//! [`engine::run`] drives one strategy over loaded bars into a ledger and its metrics,
//! and [`sweep`] runs a parameter grid of such runs under rayon over bars loaded once.
//! It records every trial in an append-only JSON Lines registry and reports the
//! deflated Sharpe and the probability of backtest overfitting across the sweep.
//!
//! The `cli` module (the default `cli` feature) is the command line: it runs a sweep spec
//! into a registry and reports a sweep from a shell. The `tradedesk-backtest` binary is
//! that command line with an empty strategy registry; a strategy crate builds its own
//! binary on `cli::main` with its strategies registered. The crate README's "Command
//! line" has its stream contract and exit codes. The library itself never needs the
//! command line's dependencies.
//!
//! Cut from tradedesk-miner (`miner-backtest`), without its strategy implementations
//! and their parity harness.
#![cfg_attr(test, allow(clippy::float_cmp))]

// The crate's own unit tests drive sweeps with the one test strategy the integration
// tests use; the file is shared, and names the crate as they do.
#[cfg(test)]
extern crate self as tradedesk_backtest;
#[cfg(test)]
#[path = "../tests/toy/mod.rs"]
pub(crate) mod toy;

pub mod aggregate;
pub mod bar;
#[cfg(feature = "cli")]
pub mod cli;
pub mod cost;
pub mod engine;
pub mod exit;
pub mod indicators;
pub mod join;
pub mod ledger;
pub mod load;
pub mod metrics;
pub mod series;
pub mod strategy;
pub mod sweep;
pub mod timeframe;

pub use aggregate::{JoinedAggregateError, aggregate_series};
pub use bar::{JoinedBar, Ohlcv};
pub use cost::config::{
    AssetClass, Carry, Commission, ComponentProvenance, ConfigError, CostComponent, CostConfig,
    Exclusion, ExclusionEffect, Financing, InstrumentSpec, Placeholder, PriceSide, Product,
    ProvenanceStatus, RolloverSpec, Slippage, Spread, SpreadBasis, SpreadWindow, TripleDay, Venue,
    VenueInstrumentCost, WeekendRule, WeekendRules,
};
pub use cost::fill::{FillError, FillModel, FillQuote, PricePoint, TradeSide};
pub use cost::financing::{Direction, Rollover, RolloverCalendar, rollover_charge};
pub use engine::{
    EndOfRun, EndOfRunRule, EngineConfig, EngineError, EngineStats, FinalBarEntry, RunOutput,
    SizingPolicy,
};
pub use exit::{BothHit, ExitEvaluation, ExitReason, ExitTrigger, SignalExitReason};
pub use join::{JoinError, Joined, OneSidedMinute, join_sides};
pub use ledger::{
    AccountFx, ClosedTrade, Fill, FillAt, FinancingCharge, Ledger, LedgerError, MarkSource,
    OpenOrder, OpenPosition, PositionId, PositionMark, RunMetadata,
};
pub use load::{InstrumentSeries, LoadError, LoadRequest, MarketData, OneSidedPolicy, load};
pub use metrics::{
    Conventions, CostBreakdown, DayConvention, Drawdown, EquityPoint, ExitReasonBreakdown,
    MarkConvention, MaxDrawdown, Metrics, MetricsConfig, MetricsError, OpenAtEnd,
    OpenPositionValue, PointKind, ReasonStats, ReturnStats, TradeStats,
};
pub use series::{JoinedSeries, SeriesError};
pub use timeframe::BarTimeframe;
