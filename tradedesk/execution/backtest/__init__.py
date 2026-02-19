"""Backtesting provider implementation."""

from .client import BacktestClient
from .streamer import BacktestStreamer, CandleSeries, MarketSeries

__all__ = [
    "BacktestClient",
    "BacktestStreamer",
    "CandleSeries",
    "MarketSeries",
]

# Removed exports (moved to recording domain):
# - CandleIndex, Excursions, build_candle_index, compute_excursions → tradedesk.recording.excursions
# - BacktestRecorder, ProgressLogger, TrackerSync → tradedesk.recording.recorders
# - BacktestSpec, run_backtest → will be replaced by new runner
