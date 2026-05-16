"""Domain events emitted by strategies.

Defines ``SignalGeneratedEvent``, published whenever a strategy produces a
trading ``Signal`` for a given instrument; downstream consumers turn signals
into order requests or analytics.
"""

from tradedesk.events import DomainEvent, event

from .base import Signal


@event
class SignalGeneratedEvent(DomainEvent):
    strategy_id: str
    instrument: str
    signal: Signal
