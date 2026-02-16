# tradedesk/__init__.py
"""
Tradedesk - Trading infrastructure library for IG Markets.
Copyright 2026 Radius Red Ltd.

Provides authenticated API access, Lightstreamer streaming, and a base
framework for implementing trading strategies.
"""

from .runner import run_strategies
from .types import (
    Candle,
    DataProvider,
    Direction,
    OrderRequest,
    OrderResult,
    StreamConsumer,
)

__version__ = "0.4.0"

__all__ = [
    "__version__",
    "Candle",
    "DataProvider",
    "Direction",
    "OrderRequest",
    "OrderResult",
    "StreamConsumer",
    "run_strategies",
]
