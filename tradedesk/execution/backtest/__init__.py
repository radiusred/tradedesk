"""Backtesting provider implementation.

This backtester is the path on which a strategy and its portfolio run byte-identically
in backtest and live: the same strategy and portfolio code drives
:func:`run_backtest` and the live runner. The Rust backtester, the
``tradedesk-backtest`` crate (``crates/tradedesk-backtest`` in this repository), is the
research engine for parameter sweeps. A later milestone joins the two, by moving the
live runtime to Rust, once a strategy has proved itself in backtesting
(Decision: https://github.com/radiusred/trading-hub/issues/2#issuecomment-6083458251).
"""

from .client import BacktestClient, FinancingCosts, TransactionCosts
from .dukascopy import iter_dukascopy_candles, read_dukascopy_candles
from .runner import BacktestSpec, run_backtest
from .streamer import BacktestStreamer, CandleSeries, MarketSeries

__all__ = [
    "BacktestClient",
    "BacktestSpec",
    "FinancingCosts",
    "BacktestStreamer",
    "CandleSeries",
    "MarketSeries",
    "TransactionCosts",
    "iter_dukascopy_candles",
    "read_dukascopy_candles",
    "run_backtest",
]
