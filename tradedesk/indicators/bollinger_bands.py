"""Bollinger Bands indicator implementation."""
import math
from collections import deque

from tradedesk.marketdata import Candle
from .base import Indicator


class BollingerBands(Indicator):
    """
    Bollinger Bands (rolling SMA +/- k * population standard deviation).

    Returns a dict:
      - middle: SMA
      - upper:  middle + k * std
      - lower:  middle - k * std
      - std:    population std (ddof=0)

    Notes:
    - Uses population standard deviation (ddof=0), which matches the common
      "platform default" behavior.
    """

    def __init__(self, period: int = 20, k: float = 2.0):
        if period <= 0:
            raise ValueError("period must be > 0")
        if k <= 0:
            raise ValueError("k must be > 0")
        self.period = period
        self.k = float(k)
        self._closes: deque[float] = deque(maxlen=period)

    def update(self, candle: Candle) -> dict[str, float | None]:
        self._closes.append(float(candle.close))

        if not self.ready():
            return {"middle": None, "upper": None, "lower": None, "std": None}

        mean = sum(self._closes) / self.period
        var = sum((x - mean) ** 2 for x in self._closes) / self.period  # ddof=0
        std = math.sqrt(var)

        upper = mean + self.k * std
        lower = mean - self.k * std

        return {"middle": mean, "upper": upper, "lower": lower, "std": std}

    def ready(self) -> bool:
        return len(self._closes) >= self.period

    def reset(self) -> None:
        self._closes.clear()

    def warmup_periods(self) -> int:
        return self.period
