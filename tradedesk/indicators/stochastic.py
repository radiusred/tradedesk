"""Stochastic Oscillator (%K / %D) indicator implementation."""

from collections import deque

from tradedesk.marketdata import Candle
from .base import Indicator


class Stochastic(Indicator):
    """
    Stochastic Oscillator.

    %K = 100 * (close - lowest_low) / (highest_high - lowest_low) over k_period
    %D = SMA(%K, d_period)

    Returns dict:
      {"k": float|None, "d": float|None}

    Notes:
    - If highest_high == lowest_low, %K is defined as 0.0 (avoids division-by-zero).
    - `ready()` becomes True only when %D is available (full warmup complete).
    """

    def __init__(self, k_period: int = 14, d_period: int = 3):
        if k_period <= 0:
            raise ValueError("k_period must be > 0")
        if d_period <= 0:
            raise ValueError("d_period must be > 0")

        self.k_period = k_period
        self.d_period = d_period

        self._highs: deque[float] = deque(maxlen=k_period)
        self._lows: deque[float] = deque(maxlen=k_period)
        self._closes: deque[float] = deque(maxlen=k_period)

        self._ks: deque[float] = deque(maxlen=d_period)

    def update(self, candle: Candle) -> dict[str, float | None]:
        self._highs.append(float(candle.high))
        self._lows.append(float(candle.low))
        self._closes.append(float(candle.close))

        if len(self._closes) < self.k_period:
            return {"k": None, "d": None}

        highest_high = max(self._highs)
        lowest_low = min(self._lows)
        close = self._closes[-1]

        denom = highest_high - lowest_low
        k = 0.0 if denom == 0.0 else 100.0 * (close - lowest_low) / denom

        self._ks.append(k)

        if len(self._ks) < self.d_period:
            return {"k": k, "d": None}

        d = sum(self._ks) / self.d_period
        return {"k": k, "d": d}

    def ready(self) -> bool:
        return len(self._closes) >= self.k_period and len(self._ks) >= self.d_period

    def reset(self) -> None:
        self._highs.clear()
        self._lows.clear()
        self._closes.clear()
        self._ks.clear()

    def warmup_periods(self) -> int:
        # Need k_period candles for first %K, then d_period %K values for %D
        return self.k_period + self.d_period - 1
