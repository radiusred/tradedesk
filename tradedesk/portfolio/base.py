"""Base classes for portfolio-level orchestration."""

import asyncio
import logging
from abc import ABC, abstractmethod
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

    def __init__(self, client: Client) -> None:
        self._client = client
        self.last_update = datetime.now(timezone.utc)
        self.subscriptions: list[MarketSubscription | ChartSubscription] = []
        self.watchdog_threshold: float = 60.0

    @abstractmethod
    async def on_candle_close(self, event: CandleClosedEvent) -> None:
        """Process a completed candle. Subclasses delegate to PortfolioRunner or
        individual strategy on_candle_close as appropriate."""
        ...

    async def on_price_update(self, data: MarketData) -> None:
        """Handle a tick-level price update. Subclasses may override."""
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
        _order_handler = OrderExecutionHandler(self._client)  # noqa: F841
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
