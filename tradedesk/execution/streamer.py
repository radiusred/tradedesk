import abc

from tradedesk.types import StreamConsumer


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
    async def run(self, consumer: StreamConsumer) -> None:
        """Run the stream and dispatch events into the supplied consumer."""
        raise NotImplementedError
