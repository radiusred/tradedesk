# tradedesk/execution/backtest/runner.py
"""Event-driven backtest runner."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from tradedesk.portfolio.base import BasePortfolio

from tradedesk.recording import (
    EquityRecorder,
    ExcursionComputer,
    Metrics,
    ProgressLogger,
    TradeLedger,
    build_candle_index,
    compute_metrics,
    register_recording_subscriber,
    trade_rows_from_trades,
)

from .client import BacktestClient


@dataclass(frozen=True)
class BacktestSpec:
    """Configuration for a backtest run."""

    instrument: str
    period: str
    cache_dir: Path
    symbol: str
    date_from: date
    date_to: date
    price_side: str = "bid"
    size: float = 1.0
    half_spread_adjustment: float = 0.0
    reporting_scale: float = 1.0


async def run_backtest(
    *,
    spec: BacktestSpec,
    out_dir: Path,
    portfolio_factory: Callable[[BacktestClient], "BasePortfolio"],
) -> Metrics:
    """
    Event-driven backtest runner.

    Sets up recording infrastructure, then delegates the full session lifecycle
    to the portfolio (warmup via SessionStartedEvent, streaming, SessionEndedEvent).

    Args:
        spec: BacktestSpec with instrument, period, cache location and date range.
        out_dir: Directory to write CSV artefacts.
        portfolio_factory: Callable that receives a BacktestClient and returns
            an initialised BasePortfolio.

    Returns:
        Metrics object with performance statistics.
    """
    raw_client = BacktestClient.from_dukascopy_cache(
        spec.cache_dir,
        symbol=spec.symbol,
        instrument=spec.instrument,
        period=spec.period,
        date_from=spec.date_from,
        date_to=spec.date_to,
        price_side=spec.price_side,
    )
    await raw_client.start()

    # Apply half-spread adjustment if specified (e.g., BID -> MID normalization)
    adj = float(spec.half_spread_adjustment or 0.0)
    if adj:
        streamer = raw_client.get_streamer()
        for series in streamer._candle_series:
            for c in series.candles:
                c.open += adj
                c.high += adj
                c.low += adj
                c.close += adj

    # Create ledger and register recording subscriber
    ledger = TradeLedger()
    _subscriber = register_recording_subscriber(ledger=ledger, output_dir=out_dir)

    # Create event-driven recorders
    _equity_recorder = EquityRecorder(raw_client, target_period=spec.period)
    _progress_logger = ProgressLogger(target_period=spec.period)

    # Build candle index for excursion tracking
    streamer = raw_client.get_streamer()
    all_candles = []
    for series in streamer._candle_series:
        if series.instrument == spec.instrument and series.period == spec.period:
            all_candles.extend(series.candles)
    candle_index = build_candle_index(all_candles)
    _excursion_computer = ExcursionComputer(candle_index)

    # Create portfolio and run the full session lifecycle
    portfolio = portfolio_factory(raw_client)
    await portfolio.run()

    # Compute and return metrics
    equity_rows = [{"timestamp": e.timestamp, "equity": str(e.equity)} for e in ledger.equity]
    trade_rows = trade_rows_from_trades(ledger.trades)

    return compute_metrics(
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        reporting_scale=float(spec.reporting_scale),
    )
