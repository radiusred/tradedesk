"""
Provider-agnostic interfaces.

This module defines the stable interfaces used by strategies and runners.
Concrete provider implementations (e.g. IG) should implement these contracts.
"""

from .backtest.client import BacktestClient
from .broker import (
    AccountBalance,
    BrokerPosition,
    DealRejectedException,
    HistoricalDataAllowanceError,
)
from .client import Client
from .events import OrderCompletedEvent, OrderRequestEvent
from .order_handler import OrderExecutionHandler, request_order
from .position import PositionTracker
from .streamer import Streamer

__all__ = [
    "AccountBalance",
    "BacktestClient",
    "BrokerPosition",
    "Client",
    "DealRejectedException",
    "HistoricalDataAllowanceError",
    "OrderCompletedEvent",
    "OrderExecutionHandler",
    "OrderRequestEvent",
    "PositionTracker",
    "request_order",
    "Streamer",
]
