# tradedesk/__init__.py
"""
Tradedesk - Trading infrastructure library for algorithmic trading strategies.
Copyright 2026 Radius Red Ltd.

Provides authenticated API access, Lightstreamer streaming, and a base
framework for implementing trading strategies.
"""

from .events import (
    DomainEvent,
    SessionEndedEvent,
    SessionReadyEvent,
    SessionStartedEvent,
    event,
    get_dispatcher,
)
from .portfolio import BasePortfolio, Portfolio, SimplePortfolio
from .runner import run_portfolio
from .types import (
    Candle,
    DataProvider,
    Direction,
    OrderRequest,
    OrderResult,
    StreamConsumer,
)

__version__ = "1.3.0"

__all__ = [
    "__version__",
    "BasePortfolio",
    "Candle",
    "DataProvider",
    "Direction",
    "DomainEvent",
    "OrderRequest",
    "OrderResult",
    "Portfolio",
    "SessionEndedEvent",
    "SessionReadyEvent",
    "SessionStartedEvent",
    "SimplePortfolio",
    "StreamConsumer",
    "event",
    "get_dispatcher",
    "run_portfolio",
]
