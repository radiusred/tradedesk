from dataclasses import dataclass

from tradedesk.events import DomainEvent

from .base import Signal


@dataclass(frozen=True)
class SignalGeneratedEvent(DomainEvent):
    strategy_id: str
    instrument: str
    signal: Signal
