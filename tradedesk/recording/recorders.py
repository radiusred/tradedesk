"""Backtest observers – collaborators extracted from the orchestrator.

Each class handles a single concern that was previously inline in
``PortfolioOrchestrator``.  They are designed as thin, stateful objects
that the orchestrator delegates to on each candle close.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tradedesk import DomainEvent
from tradedesk.recording.ledger import TradeLedger, trade_rows_from_trades
from tradedesk.recording.metrics import RoundTrip, round_trips_from_fills
from tradedesk.recording.types import EquityRecord
from tradedesk.time_utils import parse_timestamp

from .equity import compute_equity

if TYPE_CHECKING:
    from tradedesk import Candle

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Recording
# ---------------------------------------------------------------------------


class BacktestRecorder:
    """Records opportunity snapshots and equity samples during a backtest.

    Can optionally self-subscribe to CandleClosedEvent when target_period and
    client are provided during initialization.
    """

    def __init__(
        self,
        ledger: TradeLedger,
        *,
        target_period: str | None = None,
        client: Any | None = None,
    ) -> None:
        self._ledger = ledger
        self._target_period = target_period
        self._client = client

        # Self-subscribe to events if both target_period and client provided
        if target_period is not None and client is not None:
            from tradedesk.events import get_dispatcher
            from tradedesk.marketdata.events import CandleClosedEvent

            dispatcher = get_dispatcher()
            dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
            log.debug(
                "BacktestRecorder subscribed to CandleClosedEvent (target_period=%s)",
                target_period,
            )

    def _on_candle_closed(self, event: DomainEvent) -> None:
        """Handle target-period candle events for equity sampling."""
        from tradedesk.marketdata.events import CandleClosedEvent

        if (
            isinstance(event, CandleClosedEvent)
            and self._target_period is not None
            and event.timeframe == self._target_period
        ):
            self.sample_equity(event.candle, self._client)

    def sample_equity(self, candle: Candle, client: Any) -> None:
        """Sample current equity from the backtest client into the ledger."""
        inner = getattr(client, "_inner", None)
        if inner is None:
            return
        eq = compute_equity(inner)
        ts = candle.candle_with_iso_timestamp().timestamp
        self._ledger.record_equity(EquityRecord(timestamp=ts, equity=float(eq)))


# ---------------------------------------------------------------------------
# Progress logging
# ---------------------------------------------------------------------------


class ProgressLogger:
    """Logs a message at the start of each new ISO week during a backtest.

    Can optionally self-subscribe to CandleClosedEvent when target_period is
    provided during initialization.
    """

    def __init__(self, *, target_period: str | None = None) -> None:
        self._last_logged_week: tuple[int, int] | None = None
        self._target_period = target_period

        # Self-subscribe to events if target_period provided
        if target_period is not None:
            from tradedesk.events import get_dispatcher
            from tradedesk.marketdata.events import CandleClosedEvent

            dispatcher = get_dispatcher()
            dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
            log.debug(
                "ProgressLogger subscribed to CandleClosedEvent (target_period=%s)",
                target_period,
            )

    def _on_candle_closed(self, event: DomainEvent) -> None:
        """Handle target-period candle events for progress logging."""
        from tradedesk.marketdata.events import CandleClosedEvent

        if (
            isinstance(event, CandleClosedEvent)
            and self._target_period is not None
            and event.timeframe == self._target_period
        ):
            self.on_candle(event.candle)

    def on_candle(self, candle: Candle) -> None:
        dt = parse_timestamp(candle.timestamp)
        year_week = (dt.year, dt.isocalendar()[1])
        if self._last_logged_week != year_week:
            log.info(
                "Backtest progress: Week %d/%d (%s)",
                year_week[1],
                year_week[0],
                dt.strftime("%Y-%m-%d"),
            )
            self._last_logged_week = year_week


# ---------------------------------------------------------------------------
# Policy tracker synchronisation
# ---------------------------------------------------------------------------


class TrackerSync:
    """Incrementally syncs completed round-trips to the policy tracker.

    Can optionally self-subscribe to CandleClosedEvent when target_period is
    provided during initialization.
    """

    def __init__(
        self, ledger: TradeLedger, policy: Any, *, target_period: str | None = None
    ) -> None:
        self._ledger = ledger
        self._policy = policy
        self._target_period = target_period
        self._last_extracted_trade_count: int = 0
        self._all_round_trips: list[RoundTrip] = []

        # Self-subscribe to events if target_period provided
        if target_period is not None:
            from tradedesk.events import get_dispatcher
            from tradedesk.marketdata.events import CandleClosedEvent

            dispatcher = get_dispatcher()
            dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
            log.debug(
                "TrackerSync subscribed to CandleClosedEvent (target_period=%s)",
                target_period,
            )

    def _on_candle_closed(self, event: DomainEvent) -> None:
        """Handle target-period candle events for tracker sync."""
        from tradedesk.marketdata.events import CandleClosedEvent

        if (
            isinstance(event, CandleClosedEvent)
            and self._target_period is not None
            and event.timeframe == self._target_period
        ):
            self.sync()

    def sync(self) -> None:
        """Push new round-trips (if any) into the policy's tracker."""
        tracker = getattr(self._policy, "tracker", None)
        if tracker is None:
            return

        current_count = len(self._ledger.trades)
        if current_count < self._last_extracted_trade_count + 10:
            return

        all_rows = trade_rows_from_trades(self._ledger.trades)
        all_rts = round_trips_from_fills(all_rows)

        new_rts = all_rts[len(self._all_round_trips) :]
        self._all_round_trips = all_rts
        self._last_extracted_trade_count = current_count

        if not new_rts:
            return

        trades = []
        for rt in new_rts:
            entry_dt = parse_timestamp(rt.entry_ts)
            exit_dt = parse_timestamp(rt.exit_ts)
            trades.append(
                {
                    "instrument": rt.instrument,
                    "pnl": float(rt.pnl),
                    "entry_ts": rt.entry_ts,
                    "exit_ts": rt.exit_ts,
                    "hold_minutes": (exit_dt - entry_dt).total_seconds() / 60.0,
                }
            )

        tracker.update_from_trades(trades)
        log.debug(
            "Updated tracker with %d new round trips (total: %d)",
            len(trades),
            len(all_rts),
        )
