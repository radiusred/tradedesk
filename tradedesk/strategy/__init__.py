"""Strategy primitives: ``BaseStrategy`` and the dispatched ``Signal`` enum.

Strategies subclass :class:`BaseStrategy`, declare ``SUBSCRIPTIONS`` for the
market/chart streams they need, and override :meth:`on_price_update` and/or
:meth:`on_candle_close` to implement trading logic.
"""

from .base import BaseStrategy, Signal
from .events import SignalGeneratedEvent

__all__ = [
    "BaseStrategy",
    "Signal",
    "SignalGeneratedEvent",
]
