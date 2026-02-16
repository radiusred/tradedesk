from tradedesk.events import DomainEvent, event
from tradedesk.types import Candle

from .instrument import MarketData


@event
class CandleClosedEvent(DomainEvent):
    instrument: str
    timeframe: str
    candle: Candle


@event
class MarketDataReceivedEvent(DomainEvent):
    """Event emitted when tick-level market data is received."""

    data: MarketData
