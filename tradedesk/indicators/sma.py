"""Simple Moving Average (SMA) indicator implementation."""

from collections import deque

from tradedesk.marketdata import Candle
from .base import Indicator


class SMA(Indicator):
    """Simple Moving Average of close prices."""

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("period must be > 0")
        self.period = period
        self._closes: deque[float] = deque(maxlen=period)

    def update(self, candle: Candle) -> float | None:
        self._closes.append(float(candle.close))

        if not self.ready():
            return None

        return sum(self._closes) / self.period

    def ready(self) -> bool:
        return len(self._closes) >= self.period

    def reset(self) -> None:
        self._closes.clear()

    def warmup_periods(self) -> int:
        return self.period
