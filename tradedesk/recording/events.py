from tradedesk.events import DomainEvent, event


@event
class ReportingCompleteEvent(DomainEvent):
    """Emitted when session reporting is complete."""

    pass


@event
class PositionOpenedEvent(DomainEvent):
    """Emitted when a new position is opened."""

    instrument: str
    direction: str  # "BUY" or "SELL"
    size: float
    entry_price: float


@event
class PositionClosedEvent(DomainEvent):
    """Emitted when a position is fully closed."""

    instrument: str
    direction: str  # "BUY" or "SELL" (the direction of the position that was closed)
    size: float
    entry_price: float
    exit_price: float
    pnl: float
    exit_reason: str


@event
class EquitySampledEvent(DomainEvent):
    """Emitted when portfolio equity is sampled."""

    equity: float
    realised_pnl: float
    unrealised_pnl: float


@event
class ExcursionSampledEvent(DomainEvent):
    """Emitted when MFE/MAE excursions are computed for an open position."""

    instrument: str
    mfe_points: float  # Maximum Favorable Excursion in points
    mae_points: float  # Maximum Adverse Excursion in points
    mfe_pnl: float  # MFE scaled by position size
    mae_pnl: float  # MAE scaled by position size
