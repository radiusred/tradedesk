"""Commodity Channel Index (CCI) indicator implementation."""

from collections import deque

from tradedesk.marketdata import Candle
from .base import Indicator


class CCI(Indicator):
    """
    Commodity Channel Index (CCI).

    TP  = (high + low + close) / 3
    SMA = mean(TP over period)
    MD  = mean(|TP_i - SMA|)
    CCI = (TP - SMA) / (0.015 * MD)

    Notes:
    - Uses mean deviation (not std).
    - If MD == 0, returns 0.0 to avoid division-by-zero.
    """

    def __init__(self, period: int = 20):
        if period <= 0:
            raise ValueError("period must be > 0")
        self.period = period
        self._tps: deque[float] = deque(maxlen=period)

    def update(self, candle: Candle) -> float | None:
        tp = (float(candle.high) + float(candle.low) + float(candle.close)) / 3.0
        self._tps.append(tp)

        if not self.ready():
            return None

        mean_tp = sum(self._tps) / self.period
        mean_dev = sum(abs(x - mean_tp) for x in self._tps) / self.period

        if mean_dev == 0.0:
            return 0.0

        return (tp - mean_tp) / (0.015 * mean_dev)

    def ready(self) -> bool:
        return len(self._tps) >= self.period

    def reset(self) -> None:
        self._tps.clear()

    def warmup_periods(self) -> int:
        return self.period
