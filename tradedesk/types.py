from __future__ import annotations

import copy
from dataclasses import dataclass

from tradedesk.time_utils import iso_to_ms, ms_to_iso


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

    def candle_with_ms_timestamp(self) -> Candle:
        """Return a cloned *candle* with its timestamp normalised to int milliseconds.

        Used at the boundary with :class:`CandleAggregator` which expects
        millisecond-int timestamps (IG streaming format).

        mypy strict assignment errors are ignored here intentionally to allow the
        timestamp field to be either str or int in the original Candle, and then
        normalise it to int milliseconds in the cloned Candle. Rather than widening
        the type of timestamp in the Candle class, we handle the conversion here
        and ignore the type assignment error.
        """
        c = copy.copy(self)
        ts = c.timestamp
        if isinstance(ts, (int, float)):
            c.timestamp = int(ts)
        elif isinstance(ts, str) and ts.replace(".", "", 1).lstrip("-").isdigit():
            c.timestamp = int(float(ts))  # type: ignore[assignment]
        else:
            c.timestamp = iso_to_ms(str(ts))  # type: ignore[assignment]
        return c

    def candle_with_iso_timestamp(self) -> Candle:
        """Return a cloned *candle* with its timestamp normalised to an ISO string.

        Strategies and recording always work with ISO strings; this converts
        back from milliseconds when needed (e.g. after aggregation).
        """
        c = copy.copy(self)
        ts = c.timestamp
        if isinstance(ts, str):
            if ts.replace(".", "", 1).lstrip("-").isdigit():
                c.timestamp = ms_to_iso(int(float(ts)))
            return c  # already ISO

        c.timestamp = ms_to_iso(int(ts))
        return c
