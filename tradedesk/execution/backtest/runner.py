# tradedesk/execution/backtest/runner.py
"""Event-driven backtest runner - replaces harness.py wrapper pattern."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tradedesk.events import SessionEndedEvent, SessionStartedEvent, get_dispatcher
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
from tradedesk.types import StreamConsumer

from .client import BacktestClient


@dataclass(frozen=True)
class BacktestSpec:
    """Configuration for a backtest run."""

    instrument: str
    period: str
    candle_csv: Path
    size: float = 1.0
    half_spread_adjustment: float = 0.0
    reporting_scale: float = 1.0


async def run_backtest(
    *,
    spec: BacktestSpec,
    out_dir: Path,
    strategy_factory: Callable[[BacktestClient], StreamConsumer],
) -> Metrics:
    """
    Event-driven backtest runner.

    Replaces harness.py with clean event-driven architecture:
    - No wrapping of strategy._handle_event
    - Event-driven recorders subscribe to domain events
    - Recording happens transparently through RecordingSubscriber
    - Equity sampling via EquityRecorder
    - Live excursion tracking via ExcursionComputer

    Contract:
      - Replays candles from CSV via BacktestClient/BacktestStreamer
      - Creates event-driven recorders that subscribe to events
      - Records trades/equity via RecordingSubscriber + TradeLedger
      - Writes artefacts via TradeLedger.write(out_dir)
      - Computes metrics from ledger state
      - Returns Metrics object

    Args:
        spec: BacktestSpec with instrument, period, CSV path, etc.
        out_dir: Directory to write CSV artefacts
        strategy_factory: Callable that creates a StreamConsumer given a client

    Returns:
        Metrics object with performance statistics
    """
    # Create backtest client and load candles
    raw_client = BacktestClient.from_csv(
        spec.candle_csv, instrument=spec.instrument, period=spec.period
    )
    await raw_client.start()

    # Wire event-driven order execution
    from tradedesk.execution.order_handler import OrderExecutionHandler

    _order_handler = OrderExecutionHandler(raw_client)  # noqa: F841

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

    # Create strategy
    strategy = strategy_factory(raw_client)

    # Publish session started event
    await get_dispatcher().publish(SessionStartedEvent())

    # Run backtest (streamer emits events, recorders react)
    await streamer.run(strategy)

    # Publish session ended event (triggers metrics computation)
    await get_dispatcher().publish(SessionEndedEvent())

    # Compute and return metrics
    equity_rows = [{"timestamp": e.timestamp, "equity": str(e.equity)} for e in ledger.equity]
    trade_rows = trade_rows_from_trades(ledger.trades)

    return compute_metrics(
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        reporting_scale=float(spec.reporting_scale),
    )
