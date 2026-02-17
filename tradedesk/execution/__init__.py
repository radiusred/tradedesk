"""
Provider-agnostic interfaces.

This module defines the stable interfaces used by strategies and runners.
Concrete provider implementations (e.g. IG) should implement these contracts.
"""

from .broker import (
    AccountBalance,
    BrokerPosition,
    DealRejectedException,
)
from .client import Client
from .events import OrderCompletedEvent, OrderRequestEvent
from .order_handler import request_order
from .position import PositionTracker
from .streamer import Streamer

__all__ = [
    "AccountBalance",
    "BrokerPosition",
    "Client",
    "DealRejectedException",
    "OrderCompletedEvent",
    "OrderRequestEvent",
    "PositionTracker",
    "request_order",
    "Streamer",
]
