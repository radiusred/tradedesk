from tradedesk.events import DomainEvent, event
from tradedesk.types import OrderRequest, OrderResult


@event
class OrderRequestEvent(DomainEvent):
    """Published by strategies to request order execution.

    The ``request_id`` links the event to a pending Future in the
    module-level registry so the caller can ``await`` the result.
    """

    request: OrderRequest
    request_id: str


@event
class OrderCompletedEvent(DomainEvent):
    """Published after an order has been executed (or rejected)."""

    request_id: str
    result: OrderResult
