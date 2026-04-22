"""Domain events emitted by the tradedesk recording subsystem."""

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
    strategy: str = ""
    position_id: str = ""
    raw_entry_price: float = 0.0
    entry_spread_cost: float = 0.0
    entry_slippage_cost: float = 0.0
    entry_commission_cost: float = 0.0


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
    strategy: str = ""
    position_id: str = ""
    # Cost decomposition (all default to 0; populated when bid/ask data is available)
    raw_entry_price: float = 0.0
    raw_exit_price: float = 0.0
    entry_spread_cost: float = 0.0
    exit_spread_cost: float = 0.0
    entry_slippage_cost: float = 0.0
    exit_slippage_cost: float = 0.0
    entry_commission_cost: float = 0.0
    exit_commission_cost: float = 0.0
    financing_cost: float = 0.0
    admin_cost: float = 0.0


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
