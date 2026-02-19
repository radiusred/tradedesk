from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tradedesk import SessionEndedEvent, SessionStartedEvent, get_dispatcher

from .events import (
    EquitySampledEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    ReportingCompleteEvent,
)
from .ledger import TradeLedger, trade_rows_from_trades
from .metrics import compute_metrics
from .types import EquityRecord, TradeRecord

log = logging.getLogger(__name__)


class RecordingSubscriber:
    """Subscriber that owns the `TradeLedger` and writes reports in response to domain events.

    Intended to be the single writer of ledger state and metrics during a run.
    Completely event-driven: other domains emit events, this subscriber reacts.
    """

    def __init__(
        self,
        ledger: Optional[TradeLedger] = None,
        output_dir: Optional[Path] = None,
        reporting_scale: float = 1.0,
    ) -> None:
        """Initialize the recording subscriber.

        Args:
            ledger: TradeLedger to record trades/equity (created if None)
            output_dir: Base directory for timestamped run outputs
            reporting_scale: Scale factor for metrics reporting
        """
        self.ledger = ledger or TradeLedger()
        self._base_output_dir = output_dir
        self._reporting_scale = reporting_scale
        self._run_output_dir: Path | None = None
        # Track open positions for round trip pairing
        self._open_positions: dict[str, PositionOpenedEvent] = {}

    def handle_session_started(self, event: SessionStartedEvent) -> None:
        """Handle session start: create timestamped output directory."""
        if self._base_output_dir is None:
            return

        # Create a timestamped subdirectory for this run
        timestamp_str = event.timestamp.strftime("%Y%m%d_%H%M%S")
        self._run_output_dir = self._base_output_dir / timestamp_str
        self._run_output_dir.mkdir(parents=True, exist_ok=True)
        log.info(f"Recording session started: output_dir={self._run_output_dir}")

    async def handle_session_ended(self, event: SessionEndedEvent) -> None:
        """Handle session end: write metrics, files, and emit completion event."""
        log.info("Recording session ended: writing metrics and reports")

        # Write ledger files if we have an output directory
        if self._run_output_dir is not None:
            try:
                self.ledger.write(self._run_output_dir)
                log.info(f"Ledger files written to {self._run_output_dir}")
            except Exception:
                log.exception("Failed to write ledger files")

        # Compute and log metrics
        if self.ledger.trades:
            try:
                equity_rows = [
                    {"timestamp": e.timestamp, "equity": str(e.equity)}
                    for e in self.ledger.equity
                ]
                trade_rows = trade_rows_from_trades(self.ledger.trades)

                metrics = compute_metrics(
                    equity_rows=equity_rows,
                    trade_rows=trade_rows,
                    reporting_scale=self._reporting_scale,
                )

                log.info(
                    f"Session metrics: "
                    f"trades={metrics.trades} round_trips={metrics.round_trips} "
                    f"win_rate={metrics.win_rate:.1%} "
                    f"final_equity={metrics.final_equity:.2f} "
                    f"max_dd={metrics.max_drawdown:.2f}"
                )
            except Exception:
                log.exception("Failed to compute metrics")
        else:
            log.info("No trades recorded in session")

        # Emit completion event
        await get_dispatcher().publish(ReportingCompleteEvent())

    async def handle_position_opened(self, event: PositionOpenedEvent) -> None:
        """Handle position opened: track for round trip pairing."""
        self._open_positions[event.instrument] = event
        log.debug(f"Position opened: {event.instrument} {event.direction} size={event.size}")

    async def handle_position_closed(self, event: PositionClosedEvent) -> None:
        """Handle position closed: create trade records for entry and exit."""
        # Remove from open positions
        opened_event = self._open_positions.pop(event.instrument, None)

        if opened_event is None:
            log.warning(
                f"Position closed event received for {event.instrument} but no "
                f"corresponding open event found. Recording exit only."
            )

        # Record entry trade (if we have the open event)
        if opened_event:
            entry_trade = TradeRecord(
                timestamp=opened_event.timestamp.isoformat(),
                instrument=event.instrument,
                direction=event.direction,  # BUY or SELL
                size=event.size,
                price=event.entry_price,
                reason="entry",
            )
            self.ledger.record_trade(entry_trade)

        # Record exit trade
        exit_direction = "SELL" if event.direction == "BUY" else "BUY"
        exit_trade = TradeRecord(
            timestamp=event.timestamp.isoformat(),
            instrument=event.instrument,
            direction=exit_direction,
            size=event.size,
            price=event.exit_price,
            reason=event.exit_reason,
        )
        self.ledger.record_trade(exit_trade)

        log.debug(
            f"Position closed: {event.instrument} pnl={event.pnl:.2f} "
            f"reason={event.exit_reason}"
        )

    async def handle_equity_sampled(self, event: EquitySampledEvent) -> None:
        """Handle equity sampled: record to ledger."""
        equity_record = EquityRecord(
            timestamp=event.timestamp.isoformat(),
            equity=event.equity,
        )
        self.ledger.record_equity(equity_record)


def register_recording_subscriber(
    ledger: Optional[TradeLedger] = None,
    output_dir: Optional[Path] = None,
    reporting_scale: float = 1.0,
) -> RecordingSubscriber:
    """Create and register a `RecordingSubscriber` with the global dispatcher.

    Args:
        ledger: Optional TradeLedger instance (created if None)
        output_dir: Optional base directory for timestamped run outputs
        reporting_scale: Scale factor for metrics reporting

    Returns:
        The subscriber instance (useful in tests to inspect ledger state).
    """
    dispatcher = get_dispatcher()
    sub = RecordingSubscriber(
        ledger=ledger,
        output_dir=output_dir,
        reporting_scale=reporting_scale,
    )

    dispatcher.subscribe(SessionStartedEvent, sub.handle_session_started)
    dispatcher.subscribe(PositionOpenedEvent, sub.handle_position_opened)
    dispatcher.subscribe(PositionClosedEvent, sub.handle_position_closed)
    dispatcher.subscribe(EquitySampledEvent, sub.handle_equity_sampled)
    dispatcher.subscribe(SessionEndedEvent, sub.handle_session_ended)

    return sub
