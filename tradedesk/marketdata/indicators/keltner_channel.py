"""Keltner Channel indicator implementation."""

from tradedesk.types import Candle

from .atr import ATR
from .base import Indicator
from .ema import EMA


class KeltnerChannel(Indicator):
    """
    Keltner Channel: EMA(period) +/- mult * ATR(period).

    Returns a dict:
      - middle: EMA value
      - upper:  middle + mult * ATR
      - lower:  middle - mult * ATR

    Both EMA and ATR share the same period.  The channel is ready once
    both sub-indicators are ready.
    """

    def __init__(self, period: int = 20, mult: float = 1.5) -> None:
        if period <= 0:
            raise ValueError("period must be > 0")
        if mult <= 0:
            raise ValueError("mult must be > 0")
        self.period = period
        self.mult = float(mult)
        self._ema = EMA(period)
        self._atr = ATR(period)

    def update(self, candle: Candle) -> dict[str, float | None]:
        ema_val = self._ema.update(candle)
        atr_val = self._atr.update(candle)

        if ema_val is None or atr_val is None:
            return {"middle": None, "upper": None, "lower": None}

        upper = ema_val + self.mult * atr_val
        lower = ema_val - self.mult * atr_val
        return {"middle": ema_val, "upper": upper, "lower": lower}

    def ready(self) -> bool:
        return self._ema.ready() and self._atr.ready()

    def reset(self) -> None:
        self._ema.reset()
        self._atr.reset()

    def warmup_periods(self) -> int:
        return max(self._ema.warmup_periods(), self._atr.warmup_periods())
