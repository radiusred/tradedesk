"""
Provider-neutral interfaces.

The intent is to keep tradedesk strategies independent from any single broker.
At this stage the interfaces are intentionally small; we will extend them as
we encapsulate streaming and implement backtesting.
"""

import abc
from typing import Any, TYPE_CHECKING
from tradedesk.marketdata import Candle

if TYPE_CHECKING:
    from tradedesk.strategy import BaseStrategy


class Streamer(abc.ABC):
    """Abstract base for a real-time (or replay) market data stream."""

    @abc.abstractmethod
    async def connect(self) -> None:
        """Establish the underlying connection (e.g. Lightstreamer, WebSocket)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Tear down the underlying connection and unsubscribe."""
        raise NotImplementedError

    @abc.abstractmethod
    async def run(self, strategy: "BaseStrategy") -> None:
        """Run the stream and dispatch events into the supplied strategy."""
        raise NotImplementedError


class Client(abc.ABC):
    """Abstract base for broker/provider clients."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Initialise the client (e.g. create session, authenticate)."""
        raise NotImplementedError

    @abc.abstractmethod
    async def close(self) -> None:
        """Close any underlying resources."""
        raise NotImplementedError

    @abc.abstractmethod
    async def get_market_snapshot(self, epic: str) -> dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_historical_candles(
        self, epic: str, period: str, num_points: int
    ) -> list[Candle]:
        raise NotImplementedError

    @abc.abstractmethod
    async def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        currency: str = "USD",
        force_open: bool = True,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def get_streamer(self) -> Streamer:
        """Return a Streamer implementation for this client.

        Not wired in yet; will be introduced when we encapsulate Lightstreamer.
        """
        raise NotImplementedError
