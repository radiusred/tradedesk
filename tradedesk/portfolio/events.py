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
