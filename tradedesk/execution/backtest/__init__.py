"""Backtesting provider implementation."""

from .client import BacktestClient
from .dukascopy import iter_dukascopy_candles
from .runner import BacktestSpec, run_backtest
from .streamer import BacktestStreamer, CandleSeries, MarketSeries

__all__ = [
    "BacktestClient",
    "BacktestSpec",
    "BacktestStreamer",
    "CandleSeries",
    "MarketSeries",
    "iter_dukascopy_candles",
    "run_backtest",
]
