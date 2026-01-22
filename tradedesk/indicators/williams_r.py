"""Williams %R indicator implementation."""

from collections import deque
from tradedesk.marketdata import Candle
from .base import Indicator


class WilliamsR(Indicator):
    """Williams %R momentum indicator (range: -100 to 0)."""

    def __init__(self, period: int = 14):
        self.period = period
        self.highs: deque[float] = deque(maxlen=period)
        self.lows: deque[float] = deque(maxlen=period)
        self.closes: deque[float] = deque(maxlen=period)

    def update(self, candle: Candle) -> float | None:
        self.highs.append(candle.high)
        self.lows.append(candle.low)
        self.closes.append(candle.close)

        if not self.ready():
            return None

        highest_high = max(self.highs)
        lowest_low = min(self.lows)
        current_close = self.closes[-1]

        if highest_high == lowest_low:
            return -50.0

        return ((highest_high - current_close) / (highest_high - lowest_low)) * -100.0

    def ready(self) -> bool:
        return len(self.closes) >= self.period

    def reset(self) -> None:
        self.highs.clear()
        self.lows.clear()
        self.closes.clear()

    def warmup_periods(self) -> int:
        return self.period
