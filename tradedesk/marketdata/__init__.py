"""Market data primitives: subscriptions, instruments, candles, and indicators.

Provides the events and value types strategies consume from streaming feeds
(``MarketData``, ``CandleClosedEvent``, ``MarketDataReceivedEvent``) along with
helpers for chart aggregation (``CandleAggregator``), indicator warmup
bookkeeping (``Indicator``), and per-chart history (``ChartHistory``).
"""

from .aggregation import CandleAggregator, choose_base_period
from .chart_history import ChartHistory
from .events import CandleClosedEvent, MarketDataReceivedEvent
from .indicators import Indicator
from .instrument import Instrument, MarketData
from .subscriptions import (
    ChartSubscription,
    MarketSubscription,
    Subscription,
)

__all__ = [
    "CandleAggregator",
    "CandleClosedEvent",
    "MarketDataReceivedEvent",
    "choose_base_period",
    "Indicator",
    "ChartHistory",
    "ChartSubscription",
    "Instrument",
    "MarketData",
    "MarketSubscription",
    "Subscription",
]
