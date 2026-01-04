"""Average True Range (ATR) indicator implementation (Wilder)."""

from collections import deque

from tradedesk.marketdata import Candle
from .base import Indicator


class ATR(Indicator):
    """
    ATR (Average True Range) using Wilder's smoothing.

    True Range (TR):
      TR = max(
        high - low,
        abs(high - prev_close),
        abs(low - prev_close),
      )

    ATR:
      - Seed with SMA(TR) over `period`
      - Thereafter: ATR = (prev_atr * (period - 1) + TR) / period
    """

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("period must be > 0")
        self.period = period
        self._trs: deque[float] = deque(maxlen=period)
        self._prev_close: float | None = None
        self._atr: float | None = None

    def update(self, candle: Candle) -> float | None:
        high = float(candle.high)
        low = float(candle.low)
        close = float(candle.close)

        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )

        self._trs.append(tr)
        self._prev_close = close

        if not self.ready():
            return None

        if self._atr is None:
            # Seed ATR with SMA of TRs
            self._atr = sum(self._trs) / self.period
        else:
            # Wilder smoothing
            self._atr = (self._atr * (self.period - 1) + tr) / self.period

        return self._atr

    def ready(self) -> bool:
        return len(self._trs) >= self.period

    def reset(self) -> None:
        self._trs.clear()
        self._prev_close = None
        self._atr = None

    def warmup_periods(self) -> int:
        return self.period
