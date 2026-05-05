"""Strategy primitives: ``BaseStrategy`` and the dispatched ``Signal`` enum.

Strategies subclass :class:`BaseStrategy`, declare ``SUBSCRIPTIONS`` for the
market/chart streams they need, and override :meth:`on_price_update` and/or
:meth:`on_candle_close` to implement trading logic.
"""

from .base import BaseStrategy, Signal
from .events import SignalGeneratedEvent
from .ml_direction_strategy import (
    MLDirectionConfig,
    MLDirectionStrategy,
    ProbabilityModel,
    probability_to_signal,
)

__all__ = [
    "BaseStrategy",
    "MLDirectionConfig",
    "MLDirectionStrategy",
    "ProbabilityModel",
    "Signal",
    "SignalGeneratedEvent",
    "probability_to_signal",
]
