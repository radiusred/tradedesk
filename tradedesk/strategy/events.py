from tradedesk.events import DomainEvent, event

from .base import Signal


@event
class SignalGeneratedEvent(DomainEvent):
    strategy_id: str
    instrument: str
    signal: Signal
