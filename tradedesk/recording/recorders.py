"""Event-driven recorders for backtest and live trading.

Each class handles a specific recording concern through event subscription.
They are designed as thin, stateful observers that react to domain events.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from tradedesk.events import DomainEvent, get_dispatcher
from tradedesk.marketdata import CandleClosedEvent
from tradedesk.time_utils import parse_timestamp

from .events import (
    EquitySampledEvent,
    ExcursionSampledEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
)
from .excursions import CandleIndex

if TYPE_CHECKING:
    from tradedesk import Candle

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# EquityRecorder - Samples equity and publishes EquitySampledEvent
# ---------------------------------------------------------------------------


class EquityRecorder:
    """Samples portfolio equity on candle close and publishes EquitySampledEvent.

    This recorder subscribes to CandleClosedEvent for a specific target period
    and computes current equity (realised + unrealised PnL), then publishes
    an EquitySampledEvent for other subscribers to consume.
    """

    def __init__(self, client: Any, target_period: str):
        """Initialize equity recorder with auto-subscription.

        Args:
            client: BacktestClient or IGClient (any client with positions/realised_pnl)
            target_period: Only sample equity on this period's candles (e.g. "15MINUTE")
        """
        self._client = client
        self._target_period = target_period

        # Self-subscribe to CandleClosedEvent
        dispatcher = get_dispatcher()
        dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
        log.debug(f"EquityRecorder subscribed to CandleClosedEvent (target_period={target_period})")

    async def _on_candle_closed(self, event: CandleClosedEvent) -> None:
        """Handle candle close: compute and publish equity sample."""
        if event.timeframe != self._target_period:
            return

        # Compute equity using client methods
        try:
            equity = self._client.compute_equity()
            unrealised = self._client.compute_unrealised_pnl()
            realised = self._client.realised_pnl

            # Publish EquitySampledEvent
            await get_dispatcher().publish(
                EquitySampledEvent(
                    equity=equity,
                    realised_pnl=realised,
                    unrealised_pnl=unrealised,
                    timestamp=event.timestamp,
                )
            )
        except Exception:
            log.exception(f"Failed to sample equity at {event.timestamp}")


# ---------------------------------------------------------------------------
# ExcursionComputer - Computes MFE/MAE live during open positions
# ---------------------------------------------------------------------------


class ExcursionComputer:
    """Computes Maximum Favorable/Adverse Excursion live and publishes ExcursionSampledEvent.

    Subscribes to position lifecycle events and candle closes to track how far
    each open position moves in favorable and adverse directions from entry.
    """

    def __init__(self, candle_index: CandleIndex):
        """Initialize excursion computer with auto-subscription.

        Args:
            candle_index: Pre-built index of historical candles for excursion lookup
        """
        self._index = candle_index
        # Keyed by position_id so concurrent positions on the same epic (e.g.
        # dual AUDCAD sleeves) are tracked independently rather than the second
        # open overwriting the first (RAD-3756 / RAD-3727).
        self._open_positions: dict[str, PositionOpenedEvent] = {}

        # Subscribe to position lifecycle and candle events
        dispatcher = get_dispatcher()
        dispatcher.subscribe(PositionOpenedEvent, self._on_position_opened)
        dispatcher.subscribe(PositionClosedEvent, self._on_position_closed)
        dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
        log.debug("ExcursionComputer subscribed to position and candle events")

    async def _on_position_opened(self, event: PositionOpenedEvent) -> None:
        """Track newly opened position for excursion computation."""
        self._open_positions[event.position_id] = event
        log.debug(
            f"ExcursionComputer tracking: {event.instrument} ({event.position_id})"
        )

    async def _on_position_closed(self, event: PositionClosedEvent) -> None:
        """Stop tracking closed position."""
        self._open_positions.pop(event.position_id, None)

    async def _on_candle_closed(self, event: CandleClosedEvent) -> None:
        """Compute and publish excursions for every open position on this instrument.

        Each position gets its own ExcursionSampledEvent stamped with its
        position_id, so two concurrent positions on the same epic do not
        collide downstream.
        """
        # Snapshot to a list: publishing may mutate the dict via close handlers.
        positions = [
            pos
            for pos in self._open_positions.values()
            if pos.instrument == event.instrument
        ]
        for pos_event in positions:
            try:
                await self._sample_position(pos_event, event)
            except Exception:
                log.exception(
                    f"Failed to compute excursions for {event.instrument} "
                    f"({pos_event.position_id})"
                )

    async def _sample_position(
        self, pos_event: PositionOpenedEvent, event: CandleClosedEvent
    ) -> None:
        """Compute and publish a single position's MFE/MAE up to this candle."""
        # Compute MFE/MAE from entry to current candle
        entry_ts = pos_event.timestamp
        current_ts = event.timestamp

        # Find candles between entry and now
        from bisect import bisect_left, bisect_right

        i = bisect_left(self._index.ts, entry_ts)
        j = bisect_right(self._index.ts, current_ts)

        if i >= j:
            # No candles yet or alignment issue
            return

        max_high = max(self._index.high[i:j])
        min_low = min(self._index.low[i:j])

        # Compute excursion based on position direction
        if pos_event.direction == "BUY":
            mfe_points = max_high - pos_event.entry_price
            mae_points = min_low - pos_event.entry_price  # negative if adverse
        else:  # SELL
            mfe_points = pos_event.entry_price - min_low
            mae_points = pos_event.entry_price - max_high  # negative if adverse

        mfe_pnl = mfe_points * pos_event.size
        mae_pnl = mae_points * pos_event.size

        # Publish ExcursionSampledEvent
        await get_dispatcher().publish(
            ExcursionSampledEvent(
                instrument=event.instrument,
                mfe_points=float(mfe_points),
                mae_points=float(mae_points),
                mfe_pnl=float(mfe_pnl),
                mae_pnl=float(mae_pnl),
                position_id=pos_event.position_id,
                timestamp=event.timestamp,
            )
        )


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
    """Feeds completed round trips into the policy tracker as positions close.

    Subscribes to position lifecycle events to update the tracker immediately
    when positions close, replacing the old polling approach.
    """

    def __init__(self, policy: Any) -> None:
        """Initialize tracker sync with auto-subscription.

        Args:
            policy: Policy instance with a tracker attribute
        """
        self._policy = policy
        self._open_positions: dict[str, PositionOpenedEvent] = {}

        # Subscribe to position lifecycle events
        dispatcher = get_dispatcher()
        dispatcher.subscribe(PositionOpenedEvent, self._on_position_opened)
        dispatcher.subscribe(PositionClosedEvent, self._on_position_closed)
        log.debug("TrackerSync subscribed to position events")

    async def _on_position_opened(self, event: PositionOpenedEvent) -> None:
        """Track opened position for entry timestamp."""
        self._open_positions[event.instrument] = event

    async def _on_position_closed(self, event: PositionClosedEvent) -> None:
        """Update policy tracker immediately when position closes."""
        tracker = getattr(self._policy, "tracker", None)
        if tracker is None:
            return

        # Get the entry event to compute hold time
        entry_event = self._open_positions.pop(event.instrument, None)
        if entry_event is None:
            log.warning(f"No entry event found for closed position: {event.instrument}")
            return

        # Compute hold time (timestamps are already datetime objects)
        hold_minutes = (event.timestamp - entry_event.timestamp).total_seconds() / 60.0

        # Update tracker with this round trip
        trade = {
            "instrument": event.instrument,
            "pnl": float(event.pnl),
            "entry_ts": entry_event.timestamp.isoformat(),
            "exit_ts": event.timestamp.isoformat(),
            "hold_minutes": hold_minutes,
        }

        tracker.update_from_trades([trade])
        log.debug(f"Updated tracker with closed position: {event.instrument} pnl={event.pnl:.2f}")
