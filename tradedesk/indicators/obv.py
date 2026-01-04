"""On-Balance Volume (OBV) indicator implementation."""

from tradedesk.marketdata import Candle
from .base import Indicator


class OBV(Indicator):
    """
    On-Balance Volume.

    OBV starts at 0.
      - If close > prev_close: OBV += volume
      - If close < prev_close: OBV -= volume
      - If close == prev_close: OBV unchanged

    Returns:
      - None until a previous close exists (first candle)
      - Thereafter returns current OBV as float
    """

    def __init__(self):
        self._prev_close: float | None = None
        self._obv: float = 0.0

    def update(self, candle: Candle) -> float | None:
        close = float(candle.close)
        vol = float(candle.volume)

        if vol < 0:
            raise ValueError("volume must be >= 0")

        if self._prev_close is None:
            self._prev_close = close
            return None

        if close > self._prev_close:
            self._obv += vol
        elif close < self._prev_close:
            self._obv -= vol

        self._prev_close = close
        return self._obv

    def ready(self) -> bool:
        return self._prev_close is not None

    def reset(self) -> None:
        self._prev_close = None
        self._obv = 0.0

    def warmup_periods(self) -> int:
        return 2  # need two candles to compare closes
