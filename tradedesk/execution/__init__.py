"""
Provider-agnostic interfaces.

This module defines the stable interfaces used by strategies and runners.
Concrete provider implementations (e.g. IG) should implement these contracts.
"""

import importlib
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from .backtest.client import BacktestClient

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


def __getattr__(name: str) -> Any:
    """Load ``BacktestClient`` and the ``backtest`` subpackage on first use.

    ``tradedesk.execution.backtest`` is not imported with this package, so live users
    (and ``import tradedesk``) do not load the backtester.
    ``tradedesk.execution.BacktestClient`` and ``tradedesk.execution.backtest`` still
    resolve, importing the module at that point.
    """
    if name == "BacktestClient":
        from .backtest.client import BacktestClient

        return BacktestClient
    if name == "backtest":
        return importlib.import_module(f"{__name__}.backtest")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
