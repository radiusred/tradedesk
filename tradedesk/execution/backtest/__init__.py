"""Backtesting provider implementation.

Deprecated: the Rust backtester replaces this module. It is the ``tradedesk-backtest``
crate (``crates/tradedesk-backtest`` in this repository), with the market-data layer
in ``tradedesk-data``. This module keeps working and is still shipped, but new
backtests should use the Rust backtester. Importing it emits a ``DeprecationWarning``.
"""

import warnings

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

warnings.warn(
    "tradedesk.execution.backtest is deprecated: the Rust backtester (the "
    "tradedesk-backtest crate in the tradedesk repository) replaces it. It keeps "
    "working, but new backtests should use the Rust backtester.",
    DeprecationWarning,
    stacklevel=2,
)
