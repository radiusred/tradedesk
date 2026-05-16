"""Domain events for portfolio-level state changes.

Defines ``PositionUpdatedEvent`` (a per-instrument position change) and
``PortfolioValuedEvent`` (a periodic equity/cash snapshot). The thin
``Position`` placeholder lets these events be imported without pulling in the
full portfolio implementation.
"""

from ..events import DomainEvent, event

__all__ = ["DomainEvent", "event", "PositionUpdatedEvent", "PortfolioValuedEvent"]


class Position:
    pass


@event
class PositionUpdatedEvent(DomainEvent):
    instrument: str
    position: Position


@event
class PortfolioValuedEvent(DomainEvent):
    equity: float
    cash: float
