"""Base classes for portfolio-level orchestration."""

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from tradedesk.events import (
    SessionEndedEvent,
    SessionReadyEvent,
    SessionStartedEvent,
    get_dispatcher,
)
from tradedesk.execution import Client, OrderExecutionHandler
from tradedesk.marketdata import (
    CandleClosedEvent,
    ChartSubscription,
    MarketData,
    MarketDataReceivedEvent,
    MarketSubscription,
)
from tradedesk.ml.defaults import PORTFOLIO_WATCHDOG_THRESHOLD_S

log = logging.getLogger(__name__)


class Portfolio(Protocol):
    """Protocol for the top-level portfolio container."""

    async def run(self) -> None: ...


class BasePortfolio(ABC):
    """Abstract base for portfolio-level orchestrators.

    Subclasses implement on_candle_close() and compose PortfolioRunner,
    ReconciliationManager, etc. in __init__.

    The full session lifecycle is managed here:
      - SessionStartedEvent fires first (triggers strategy warmup and
        startup reconciliation for any ReconciliationManager instances)
      - SessionReadyEvent fires next (triggers post_warmup_check)
      - Streaming begins
      - SessionEndedEvent fires on exit
    """

    def __init__(
        self,
        client: Client,
        spread_limits: dict[str, float] | None = None,
        order_gate: Callable[[], str | None] | None = None,
    ) -> None:
        """Initialise the portfolio with a broker client and execution gates.

        Args:
            client: Broker client used by the embedded ``OrderExecutionHandler``
                for order placement and market snapshots.
            spread_limits: Optional mapping of instrument (epic) → maximum
                permitted raw spread. Orders for instruments with a configured
                limit are rejected when the live spread exceeds it.
            order_gate: Optional callable evaluated before every order. Return
                ``None`` to allow the order, or an error string to reject it.
                Used for pause/kill switches that must not interrupt streaming.
        """
        self._client = client
        self._spread_limits = spread_limits
        self._order_gate = order_gate
        self.last_update = datetime.now(timezone.utc)
        self.subscriptions: list[MarketSubscription | ChartSubscription] = []
        self.watchdog_threshold: float = PORTFOLIO_WATCHDOG_THRESHOLD_S

    @abstractmethod
    async def on_candle_close(self, event: CandleClosedEvent) -> None:
        """Process a completed candle for one of the portfolio's chart subscriptions.

        Subclasses typically delegate to a ``PortfolioRunner`` or to the relevant
        strategy's ``on_candle_close`` to evaluate signals and request orders.

        Args:
            event: The closed candle and its instrument/timeframe metadata.
        """
        ...

    async def on_price_update(self, data: MarketData) -> None:
        """Handle a tick-level price update for one of the portfolio's market subscriptions.

        Default implementation is a no-op. Subclasses should override to drive
        tick-level logic (e.g. trailing stops, intra-bar signals).

        Args:
            data: The latest bid/offer snapshot for a subscribed instrument.
        """
        pass

    async def _handle_event(self, event: object) -> None:
        """StreamConsumer interface — called by the streamer on each market event."""
        dispatcher = get_dispatcher()
        if isinstance(event, CandleClosedEvent):
            await dispatcher.publish(event)
            await self.on_candle_close(event)
        elif isinstance(event, MarketData):
            await dispatcher.publish(MarketDataReceivedEvent(data=event))
            await self.on_price_update(event)
        else:
            raise TypeError(f"Unsupported event type: {type(event)!r}")
        self.last_update = datetime.now(timezone.utc)

    async def run(self) -> None:
        """Full lifecycle: wire services → startup events → stream → shutdown."""
        _order_handler = OrderExecutionHandler(  # noqa: F841
            self._client,
            spread_limits=self._spread_limits,
            order_gate=self._order_gate,
        )
        try:
            await get_dispatcher().publish(SessionStartedEvent())
            await get_dispatcher().publish(SessionReadyEvent())
            await self._run_streaming()
        finally:
            await get_dispatcher().publish(SessionEndedEvent())

    async def _run_streaming(self) -> None:
        streamer = self._client.get_streamer()
        if streamer is not None:
            await streamer.run(self)
        else:
            log.info("No streamer available — polling mode")
            await self._run_polling()

    async def _run_polling(self) -> None:
        """Subclasses may override for polling fallback. Default waits forever."""
        await asyncio.Future()
