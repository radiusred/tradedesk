"""Backtesting provider implementation."""

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
