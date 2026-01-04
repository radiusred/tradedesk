"""Exponential Moving Average (EMA) indicator implementation."""

from tradedesk.marketdata import Candle
from .base import Indicator


class EMA(Indicator):
    """Exponential Moving Average of close prices."""

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("period must be > 0")
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self._ema: float | None = None
        self._count: int = 0

    def update(self, candle: Candle) -> float | None:
        close = float(candle.close)
        self._count += 1

        if self._ema is None:
            # Seed EMA with first close
            self._ema = close
        else:
            self._ema = (close - self._ema) * self.alpha + self._ema

        if not self.ready():
            return None

        return self._ema

    def ready(self) -> bool:
        return self._count >= self.period

    def reset(self) -> None:
        self._ema = None
        self._count = 0

    def warmup_periods(self) -> int:
        return self.period
