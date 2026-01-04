"""Relative Strength Index (RSI) indicator (Wilder)."""

from tradedesk.marketdata import Candle
from .base import Indicator


class RSI(Indicator):
    """
    Relative Strength Index (RSI) using Wilder's smoothing.

    RSI = 100 - (100 / (1 + RS))
    RS  = avg_gain / avg_loss

    Implementation:
      - Seed avg_gain/avg_loss with SMA of gains/losses over `period` deltas
      - Then apply Wilder smoothing thereafter
      - First RSI value is produced after period+1 candles (period deltas)
    """

    def __init__(self, period: int = 14):
        if period <= 0:
            raise ValueError("period must be > 0")
        self.period = period
        self._prev_close: float | None = None

        # Seeding
        self._seed_gain_sum: float = 0.0
        self._seed_loss_sum: float = 0.0
        self._deltas: int = 0  # number of deltas processed (candles - 1)

        # Smoothed averages (become non-None only after seeding completes)
        self._avg_gain: float | None = None
        self._avg_loss: float | None = None

    def update(self, candle: Candle) -> float | None:
        close = float(candle.close)

        if self._prev_close is None:
            self._prev_close = close
            return None

        delta = close - self._prev_close
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)

        self._deltas += 1

        # Seeding phase: accumulate sums until we have `period` deltas
        if self._avg_gain is None:
            self._seed_gain_sum += gain
            self._seed_loss_sum += loss

            if self._deltas < self.period:
                self._prev_close = close
                return None

            # Final seed: convert sums to averages
            self._avg_gain = self._seed_gain_sum / self.period
            self._avg_loss = self._seed_loss_sum / self.period

            self._prev_close = close
            return self._compute_rsi(self._avg_gain, self._avg_loss)

        # Wilder smoothing phase
        self._avg_gain = (self._avg_gain * (self.period - 1) + gain) / self.period
        self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        self._prev_close = close
        return self._compute_rsi(self._avg_gain, self._avg_loss)

    @staticmethod
    def _compute_rsi(avg_gain: float, avg_loss: float) -> float:
        if avg_loss == 0.0:
            return 100.0
        if avg_gain == 0.0:
            return 0.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def ready(self) -> bool:
        return self._avg_gain is not None and self._avg_loss is not None

    def reset(self) -> None:
        self._prev_close = None
        self._seed_gain_sum = 0.0
        self._seed_loss_sum = 0.0
        self._deltas = 0
        self._avg_gain = None
        self._avg_loss = None

    def warmup_periods(self) -> int:
        # Need period deltas → period + 1 candles
        return self.period + 1
