from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from tradedesk import OrderRequest, SessionEndedEvent, SessionStartedEvent, get_dispatcher
from tradedesk.execution import OrderCompletedEvent, OrderRequestEvent
from tradedesk.portfolio import PositionJournal

from .events import ReportingCompleteEvent
from .ledger import TradeLedger, trade_rows_from_trades
from .metrics import compute_metrics
from .types import TradeRecord

log = logging.getLogger(__name__)


class RecordingSubscriber:
    """Subscriber that owns the `TradeLedger` and writes reports in response to domain events.

    Intended to be the single writer of ledger state and metrics during a run.
    Completely event-driven: other domains emit events, this subscriber reacts.
    """

    def __init__(
        self,
        ledger: Optional[TradeLedger] = None,
        journal_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        reporting_scale: float = 1.0,
    ) -> None:
        """Initialize the recording subscriber.

        Args:
            ledger: TradeLedger to record trades/equity (created if None)
            journal_dir: Directory for position journal (portfolio domain)
            output_dir: Base directory for timestamped run outputs
            reporting_scale: Scale factor for metrics reporting
        """
        self.ledger = ledger or TradeLedger()
        self.journal: PositionJournal | None = (
            PositionJournal(journal_dir) if journal_dir is not None else None
        )
        self._base_output_dir = output_dir
        self._reporting_scale = reporting_scale
        self._run_output_dir: Path | None = None
        # Track pending order requests so we can enrich OrderCompletedEvent
        # with the original OrderRequest information.
        self._pending_requests: dict[str, OrderRequest] = {}

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

    def handle_order_request(self, event: OrderRequestEvent) -> None:
        # Store request so completed event can be correlated
        self._pending_requests[event.request_id] = event.request

    def handle_order_completed(self, event: OrderCompletedEvent) -> None:
        # Correlate to original request if available
        req = self._pending_requests.pop(event.request_id, None)
        res = event.result

        if not res.success:
            return

        instrument = None
        direction = None
        size = 0.0
        price = float(res.fill_price) if res.fill_price else 0.0

        if req is not None:
            instrument = req.instrument
            direction = req.direction
            size = float(res.fill_size) if res.fill_size else float(req.size)
        else:
            # Best-effort: try to extract from result.raw if present
            raw = getattr(res, "raw", {}) or {}
            instrument = raw.get("instrument")
            direction = raw.get("direction")
            size = float(raw.get("size", 0.0)) or float(res.fill_size)

        if instrument is None or direction is None:
            # Can't create a TradeRecord without instrument/direction
            return

        tr = TradeRecord(
            timestamp=event.timestamp.isoformat(),
            instrument=instrument,
            direction=direction,
            size=size,
            price=price,
            reason=(res.error or ""),
        )

        self.ledger.record_trade(tr)


def register_recording_subscriber(
    ledger: Optional[TradeLedger] = None,
    journal_dir: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    reporting_scale: float = 1.0,
) -> RecordingSubscriber:
    """Create and register a `RecordingSubscriber` with the global dispatcher.

    Args:
        ledger: Optional TradeLedger instance (created if None)
        journal_dir: Optional directory for position journal
        output_dir: Optional base directory for timestamped run outputs
        reporting_scale: Scale factor for metrics reporting

    Returns:
        The subscriber instance (useful in tests to inspect ledger state).
    """
    dispatcher = get_dispatcher()
    sub = RecordingSubscriber(
        ledger=ledger,
        journal_dir=journal_dir,
        output_dir=output_dir,
        reporting_scale=reporting_scale,
    )

    dispatcher.subscribe(SessionStartedEvent, sub.handle_session_started)
    dispatcher.subscribe(OrderRequestEvent, sub.handle_order_request)
    dispatcher.subscribe(OrderCompletedEvent, sub.handle_order_completed)
    dispatcher.subscribe(SessionEndedEvent, sub.handle_session_ended)

    return sub
