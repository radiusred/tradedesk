# tradedesk/chartdata.py
"""
Chart data management - candle storage and history tracking.

Provides data structures for OHLCV candles and maintains rolling
windows of historical data for indicator calculations.
"""

from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np


@dataclass(frozen=True)
class MarketData:
    """Represents a tick-level market update."""

    epic: str
    bid: float
    offer: float
    timestamp: str
    raw: dict[str, Any]


@dataclass
class Candle:
    """
    Represents a single OHLCV candle.

    Attributes:
        timestamp: ISO 8601 timestamp or Unix timestamp string
        open: Opening price
        high: Highest price during period
        low: Lowest price during period
        close: Closing price
        volume: Trading volume (or 0 if unavailable)
        tick_count: Number of ticks/updates during period
    """

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    tick_count: int = 0

    @property
    def typical_price(self) -> float:
        """
        Calculate typical price (HLC/3).
        Used in Money Flow Index and other volume-weighted indicators.
        """
        return (self.high + self.low + self.close) / 3

    @property
    def mid(self) -> float:
        """Calculate midpoint between high and low."""
        return (self.high + self.low) / 2

    @property
    def range(self) -> float:
        """Calculate candle range (high - low)."""
        return self.high - self.low

    def __repr__(self) -> str:
        return (
            f"Candle(timestamp={self.timestamp}, "
            f"O={self.open:.5f}, H={self.high:.5f}, "
            f"L={self.low:.5f}, C={self.close:.5f}, "
            f"V={self.volume:.0f})"
        )


@dataclass(frozen=True)
class CandleClose:
    """Represents a completed OHLCV candle."""

    epic: str
    period: str
    candle: Candle


class ChartHistory:
    """
    Maintains a rolling window of candles for a specific epic/timeframe pair.

    Provides convenient access to price arrays needed for indicator calculations.
    Automatically manages memory by limiting history length.

    Example:
        history = ChartHistory("CS.D.GBPUSD.TODAY.IP", "5MINUTE", max_length=200)
        history.add_candle(candle)

        # Get arrays for indicator calculation
        closes = history.get_closes()
        highs = history.get_highs(count=20)  # Last 20 candles only
    """

    def __init__(self, epic: str, period: str, max_length: int = 200):
        """
        Initialize chart history.

        Args:
            epic: Instrument identifier
            period: Timeframe (e.g., "5MINUTE", "HOUR")
            max_length: Maximum number of candles to retain
        """
        self.epic = epic
        self.period = period
        self.max_length = max_length
        self.candles: deque[Candle] = deque(maxlen=max_length)

    def add_candle(self, candle: Candle) -> None:
        """
        Add a new candle to history.

        Automatically removes oldest candle if at max_length.
        """
        self.candles.append(candle)

    def get_candles(self, count: Optional[int] = None) -> list[Candle]:
        """
        Get candle objects.

        Args:
            count: Number of most recent candles to return (None = all)

        Returns:
            List of Candle objects, oldest first
        """
        if count is None:
            return list(self.candles)
        return list(self.candles)[-count:]

    def get_opens(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of opening prices."""
        candles = self.get_candles(count)
        return np.array([c.open for c in candles], dtype=np.float64)

    def get_highs(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of high prices."""
        candles = self.get_candles(count)
        return np.array([c.high for c in candles], dtype=np.float64)

    def get_lows(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of low prices."""
        candles = self.get_candles(count)
        return np.array([c.low for c in candles], dtype=np.float64)

    def get_closes(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of closing prices."""
        candles = self.get_candles(count)
        return np.array([c.close for c in candles], dtype=np.float64)

    def get_volumes(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of volumes."""
        candles = self.get_candles(count)
        return np.array([c.volume for c in candles], dtype=np.float64)

    def get_tick_counts(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of tick counts (volume proxy for forex)."""
        candles = self.get_candles(count)
        return np.array([c.tick_count for c in candles], dtype=np.int64)

    def get_typical_prices(self, count: Optional[int] = None) -> np.ndarray:
        """Get array of typical prices (HLC/3)."""
        candles = self.get_candles(count)
        return np.array([c.typical_price for c in candles], dtype=np.float64)

    @property
    def latest(self) -> Optional[Candle]:
        """Get the most recent candle, or None if empty."""
        return self.candles[-1] if self.candles else None

    def __len__(self) -> int:
        """Return number of candles in history."""
        return len(self.candles)

    def __repr__(self) -> str:
        return (
            f"ChartHistory(epic={self.epic}, period={self.period}, "
            f"candles={len(self)}/{self.max_length})"
        )
