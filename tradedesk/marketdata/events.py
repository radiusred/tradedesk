"""Domain events emitted by the market-data pipeline.

Defines ``CandleClosedEvent`` (fired when a candle bar completes for a given
instrument/timeframe) and ``MarketDataReceivedEvent`` (fired on each tick-level
update).
"""

from ..events import DomainEvent, event
from ..types import Candle
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
