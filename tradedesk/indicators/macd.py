# tradedesk/indicators/macd.py
"""
MACD (Moving Average Convergence Divergence) indicator implementation.

MACD is a trend-following momentum indicator that shows the relationship
between two moving averages of prices.
"""

from collections import deque
from .base import Indicator
from ..marketdata import Candle


class MACD(Indicator):
    """
    MACD (Moving Average Convergence Divergence) indicator.

    Calculates the difference between two exponential moving averages (EMAs)
    and a signal line (EMA of the MACD line).

    Components:
        - MACD Line: Fast EMA - Slow EMA
        - Signal Line: EMA of MACD Line
        - Histogram: MACD Line - Signal Line

    Signals:
        - MACD crosses above signal: Bullish
        - MACD crosses below signal: Bearish
        - Histogram expanding: Trend strengthening
        - Histogram contracting: Trend weakening

    Args:
        fast: Fast EMA period (default: 12)
        slow: Slow EMA period (default: 26)
        signal: Signal line EMA period (default: 9)

    Example:
        macd = MACD(fast=12, slow=26, signal=9)

        for candle in candles:
            values = macd.update(candle)
            if macd.ready():
                if values['histogram'] > 0:
                    print("Bullish momentum")
    """

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        """
        Initialize MACD indicator.

        Args:
            fast: Fast EMA period (default: 12)
            slow: Slow EMA period (default: 26)
            signal: Signal line EMA period (default: 9)
        """
        self.fast_period = fast
        self.slow_period = slow
        self.signal_period = signal

        # Price history
        self.closes: deque[float] = deque(maxlen=slow)

        # EMA values (None until calculated)
        self.fast_ema: float | None = None
        self.slow_ema: float | None = None
        self.signal_ema: float | None = None

        # MACD line history for signal calculation
        self.macd_values: deque[float] = deque(maxlen=signal)

        # Multipliers for EMA calculation
        self.fast_multiplier = 2 / (fast + 1)
        self.slow_multiplier = 2 / (slow + 1)
        self.signal_multiplier = 2 / (signal + 1)

    def update(self, candle: Candle) -> dict[str, float | None]:
        """
        Update indicator with new candle data.

        Args:
            candle: New candle data

        Returns:
            Dictionary with 'macd', 'signal', and 'histogram' values,
            or None if not ready
        """
        close = candle.close
        self.closes.append(close)

        # Initialize EMAs with SMA when we have enough data
        if self.fast_ema is None and len(self.closes) >= self.fast_period:
            self.fast_ema = (
                sum(list(self.closes)[-self.fast_period :]) / self.fast_period
            )

        if self.slow_ema is None and len(self.closes) >= self.slow_period:
            self.slow_ema = sum(self.closes) / self.slow_period

        # Update EMAs if initialized
        if self.fast_ema is not None:
            self.fast_ema = (close * self.fast_multiplier) + (
                self.fast_ema * (1 - self.fast_multiplier)
            )

        if self.slow_ema is not None:
            self.slow_ema = (close * self.slow_multiplier) + (
                self.slow_ema * (1 - self.slow_multiplier)
            )

        # Can't calculate MACD until both EMAs are ready
        if self.fast_ema is None or self.slow_ema is None:
            return {"macd": None, "signal": None, "histogram": None}

        # Calculate MACD line
        macd_line = self.fast_ema - self.slow_ema
        self.macd_values.append(macd_line)

        # Initialize signal line with SMA when we have enough MACD values
        if self.signal_ema is None and len(self.macd_values) >= self.signal_period:
            self.signal_ema = sum(self.macd_values) / self.signal_period

        # Update signal line if initialized
        if self.signal_ema is not None:
            self.signal_ema = (macd_line * self.signal_multiplier) + (
                self.signal_ema * (1 - self.signal_multiplier)
            )

        # Can't return full values until signal is ready
        if self.signal_ema is None:
            return {"macd": None, "signal": None, "histogram": None}

        histogram = macd_line - self.signal_ema

        return {
            "macd": macd_line,
            "signal": self.signal_ema,
            "histogram": histogram,
        }

    def ready(self) -> bool:
        """
        Check if indicator has enough data.

        Returns:
            True if all components (MACD, signal, histogram) are available
        """
        return (
            self.fast_ema is not None
            and self.slow_ema is not None
            and self.signal_ema is not None
        )

    def reset(self) -> None:
        """Clear all stored data."""
        self.closes.clear()
        self.macd_values.clear()
        self.fast_ema = None
        self.slow_ema = None
        self.signal_ema = None

    def warmup_periods(self) -> int:
        return self.slow_period + self.signal_period - 1

    def __repr__(self) -> str:
        return (
            f"MACD(fast={self.fast_period}, slow={self.slow_period}, "
            f"signal={self.signal_period}, ready={self.ready()})"
        )
