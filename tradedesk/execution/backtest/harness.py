# tradedesk/execution/backtest/harness.py
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from tradedesk.recording.equity import compute_equity
from tradedesk.recording.ledger import TradeLedger
from tradedesk.recording.metrics import compute_metrics
from tradedesk.recording.types import EquityRecord
from tradedesk.types import StreamConsumer

from .client import BacktestClient


@dataclass(frozen=True)
class BacktestSpec:
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
) -> dict[str, str | int | float]:
    """
    Strategy-agnostic candle backtest runner.

    Contract:
      - Replays candles from CSV via BacktestClient/BacktestStreamer
      - Wraps strategy event handling to sample equity per event
      - Records trades via RecordingClient + TradeLedger
      - Writes artefacts via TradeLedger.write(out_dir)
      - Computes metrics from ledger state
      - Returns a flat dict row suitable for metrics.csv aggregation
    """
    from tradedesk.execution.order_handler import OrderExecutionHandler

    raw_client = BacktestClient.from_csv(
        spec.candle_csv, instrument=spec.instrument, period=spec.period
    )
    await raw_client.start()

    # Wire event-driven order execution for the backtest client.
    _order_handler = OrderExecutionHandler(raw_client)  # noqa: F841

    # Apply additive price adjustment to candle OHLC (e.g. BID -> MID normalisation).
    adj = float(spec.half_spread_adjustment or 0.0)
    if adj:
        streamer = raw_client.get_streamer()
        for series in streamer._candle_series:
            for c in series.candles:
                c.open += adj
                c.high += adj
                c.low += adj
                c.close += adj

    ledger = TradeLedger()

    strat = strategy_factory(raw_client)

    orig_handle = strat._handle_event  # StreamConsumer guarantees this

    # Wrap _handle_event to sample equity on every event.
    async def wrapped_handle(event: object) -> None:
        await orig_handle(event)

        ts = raw_client._current_timestamp or getattr(event, "timestamp", "") or ""
        ledger.record_equity(
            EquityRecord(timestamp=str(ts), equity=float(compute_equity(raw_client)))
        )

    strat._handle_event = wrapped_handle  # type: ignore[method-assign]

    streamer = raw_client.get_streamer()
    await streamer.run(strat)

    # Persist artefacts via ledger.
    out_dir.mkdir(parents=True, exist_ok=True)
    ledger.write(out_dir)

    equity_rows = [
        {"timestamp": e.timestamp, "equity": str(e.equity)} for e in ledger.equity
    ]
    trade_rows = [
        {
            "timestamp": t.timestamp,
            "instrument": t.instrument,
            "direction": t.direction,
            "size": str(t.size),
            "price": str(t.price),
        }
        for t in ledger.trades
    ]

    m = compute_metrics(
        equity_rows=equity_rows,
        trade_rows=trade_rows,
        reporting_scale=float(spec.reporting_scale),
    )

    # Preserve the existing matrix metrics schema/formatting (keeps current expectations stable).
    return {
        "instrument": spec.instrument,
        "period": spec.period,
        "fills": m.trades,
        "round_trips": m.round_trips,
        "final_equity": f"{m.final_equity:.2f}",
        "max_dd": f"{m.max_drawdown:.2f}",
        "win_rate": f"{m.win_rate * 100:.1f}",
        "avg_win": f"{m.avg_win:.2f}",
        "avg_loss": f"{m.avg_loss:.2f}",
        "profit_factor": f"{m.profit_factor:.2f}",
        "expectancy": f"{m.expectancy:.2f}",
        "avg_hold_min": f"{m.avg_hold_minutes:.1f}"
        if m.avg_hold_minutes is not None
        else "",
    }
