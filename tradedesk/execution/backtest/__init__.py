"""Backtesting provider implementation."""

from .client import BacktestClient
from .runner import BacktestSpec, run_backtest
from .streamer import BacktestStreamer, CandleSeries, MarketSeries

__all__ = [
    "BacktestClient",
    "BacktestSpec",
    "BacktestStreamer",
    "CandleSeries",
    "MarketSeries",
    "run_backtest",
]

# Removed exports (moved to recording domain):
# - CandleIndex, Excursions, build_candle_index, compute_excursions → tradedesk.recording.excursions
# - BacktestRecorder, ProgressLogger, TrackerSync → tradedesk.recording.recorders
