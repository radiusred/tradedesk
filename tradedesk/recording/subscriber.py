from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from tradedesk.events import SessionEndedEvent, SessionStartedEvent, get_dispatcher
from tradedesk.marketdata import CandleClosedEvent
from tradedesk.time_utils import parse_timestamp

from .events import (
    EquitySampledEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    ReportingCompleteEvent,
)
from .excursions import CandleIndex
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
        run_dir: Optional[Path] = None,
        index_period: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        with_graphs: bool = True,
    ) -> None:
        """Initialize the recording subscriber.

        Args:
            ledger: TradeLedger to record trades/equity (created if None)
            output_dir: Base directory for timestamped run outputs
            reporting_scale: Scale factor for metrics reporting
            run_dir: Pre-created run output directory. When provided the
                subscriber uses it directly and skips directory creation on
                SessionStartedEvent.  Caller is responsible for creating it.
            index_period: IG-format timeframe string (e.g. "15MINUTE"). When
                set, the subscriber accumulates candle data from
                CandleClosedEvent and builds ledger.candle_indices during the
                run, so callers do not need to pre-build it.
            cache_dir: Dukascopy cache directory. When provided, the analysis
                report will include a FTSE 100 control line on the equity chart.
            with_graphs: When False, graph generation is skipped in the analysis
                report (faster; useful for automated arena runs).
        """
        self.ledger = ledger or TradeLedger()
        self._base_output_dir = output_dir
        self._reporting_scale = reporting_scale
        self._run_output_dir: Path | None = run_dir
        self._index_period = index_period
        self._cache_dir = cache_dir
        self._with_graphs = with_graphs
        # Per-instrument (ts, high, low) accumulators for candle index building.
        self._index_buffer: dict[str, tuple[list[datetime], list[float], list[float]]] = {}
        # Track open positions for round trip pairing
        self._open_positions: dict[str, PositionOpenedEvent] = {}

    async def _on_candle_for_index(self, event: CandleClosedEvent) -> None:
        """Accumulate candle data for building ledger.candle_indices during streaming."""
        if event.timeframe != self._index_period:
            return
        epic = event.instrument
        if epic not in self._index_buffer:
            self._index_buffer[epic] = ([], [], [])
        ts_list, high_list, low_list = self._index_buffer[epic]
        ts_list.append(parse_timestamp(event.candle.timestamp))
        high_list.append(float(event.candle.high))
        low_list.append(float(event.candle.low))

    def handle_session_started(self, event: SessionStartedEvent) -> None:
        """Handle session start: create timestamped output directory."""
        if self._run_output_dir is not None:
            # Directory was pre-created by the caller; nothing to do.
            log.info(f"Recording session started: output_dir={self._run_output_dir}")
            return

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

        # Populate candle_indices from streaming-accumulated buffer (lazy mode).
        for epic, (ts_list, high_list, low_list) in self._index_buffer.items():
            if epic not in self.ledger.candle_indices:
                self.ledger.candle_indices[epic] = CandleIndex(
                    ts=ts_list, high=high_list, low=low_list
                )

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
                    {"timestamp": e.timestamp, "equity": str(e.equity)} for e in self.ledger.equity
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

        # Generate analysis report if we have an output directory and trades
        if self._run_output_dir is not None and self.ledger.trades:
            try:
                from .report import generate_analysis_report

                generate_analysis_report(
                    self._run_output_dir,
                    cache_dir=self._cache_dir,
                    with_graphs=self._with_graphs,
                )
                log.info(f"Analysis report written to {self._run_output_dir / 'analysis.md'}")
            except Exception:
                log.exception("Failed to generate analysis report")

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
                raw_price=event.raw_entry_price,
                spread_cost=event.entry_spread_cost,
                slippage_cost=event.entry_slippage_cost,
                commission_cost=event.entry_commission_cost,
            )
            self.ledger.record_trade(entry_trade)

        # Record exit trade (financing/admin costs attach to exit since they accrue over the hold)
        exit_direction = "SELL" if event.direction == "BUY" else "BUY"
        exit_trade = TradeRecord(
            timestamp=event.timestamp.isoformat(),
            instrument=event.instrument,
            direction=exit_direction,
            size=event.size,
            price=event.exit_price,
            reason=event.exit_reason,
            raw_price=event.raw_exit_price,
            spread_cost=event.exit_spread_cost,
            slippage_cost=event.exit_slippage_cost,
            commission_cost=event.exit_commission_cost,
            financing_cost=event.financing_cost,
            admin_cost=event.admin_cost,
        )
        self.ledger.record_trade(exit_trade)

        log.debug(
            f"Position closed: {event.instrument} pnl={event.pnl:.2f} reason={event.exit_reason}"
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
    run_dir: Optional[Path] = None,
    index_period: Optional[str] = None,
    cache_dir: Optional[Path] = None,
    with_graphs: bool = True,
) -> RecordingSubscriber:
    """Create and register a `RecordingSubscriber` with the global dispatcher.

    Args:
        ledger: Optional TradeLedger instance (created if None)
        output_dir: Optional base directory for timestamped run outputs
        reporting_scale: Scale factor for metrics reporting
        run_dir: Pre-created run output directory. When provided the subscriber
            uses it directly; ``output_dir`` is ignored.  Caller is responsible
            for creating the directory before passing it here.
        index_period: IG-format timeframe string (e.g. "15MINUTE"). When set,
            the subscriber builds ledger.candle_indices from CandleClosedEvent
            data during the run instead of requiring a pre-built index.
        cache_dir: Dukascopy cache directory. When provided, the analysis
            report will include a FTSE 100 control line on the equity chart.
        with_graphs: When False, graph generation is skipped in the analysis
            report (faster; useful for automated arena runs).

    Returns:
        The subscriber instance (useful in tests to inspect ledger state).
    """
    dispatcher = get_dispatcher()
    sub = RecordingSubscriber(
        ledger=ledger,
        output_dir=output_dir,
        reporting_scale=reporting_scale,
        run_dir=run_dir,
        index_period=index_period,
        cache_dir=cache_dir,
        with_graphs=with_graphs,
    )

    dispatcher.subscribe(SessionStartedEvent, sub.handle_session_started)
    dispatcher.subscribe(PositionOpenedEvent, sub.handle_position_opened)
    dispatcher.subscribe(PositionClosedEvent, sub.handle_position_closed)
    dispatcher.subscribe(EquitySampledEvent, sub.handle_equity_sampled)
    dispatcher.subscribe(SessionEndedEvent, sub.handle_session_ended)
    if index_period is not None:
        dispatcher.subscribe(CandleClosedEvent, sub._on_candle_for_index)

    return sub
