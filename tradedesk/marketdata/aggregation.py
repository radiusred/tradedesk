"""Candle aggregation for timeframe conversion."""

from dataclasses import dataclass
from typing import Optional

from ..types import Candle
from .timeframe import Timeframe


def period_to_seconds(period: str | Timeframe) -> int:
    """Convert a period (string or :class:`Timeframe`) to seconds.

    Falls back to ad-hoc ``NMINUTE`` parsing for any string that the enum
    doesn't recognise — preserves the old "unsupported -> ValueError" contract.
    """
    if isinstance(period, Timeframe):
        return period.to_seconds()
    try:
        return Timeframe.from_value(period).to_seconds()
    except ValueError:
        # Fall through to the legacy ad-hoc parser below for any uncommon
        # NMINUTE values (e.g. "7MINUTE") that don't have an enum member.
        pass

    p = period.strip().upper()
    if p.endswith("MINUTE"):
        try:
            n = int(p.removesuffix("MINUTE"))
        except ValueError as e:
            raise ValueError(f"Unsupported period: {period!r}") from e
        return n * 60
    raise ValueError(f"Unsupported period: {period!r}")


def choose_base_period(target_period: str, *, supported_periods: list[str] | None = None) -> str:
    """
    Choose a suitable base period for building target_period.

    Args:
        target_period: Desired aggregation period (e.g., "15MINUTE")
        supported_periods: List of broker-supported periods (default: IG scales)

    Default supported periods (IG): SECOND, 1MINUTE, 5MINUTE, HOUR

    Rule:
      - If target is >= 5 minutes and divisible by 5 minutes -> 5MINUTE
      - Else if target is >= 1 minute -> 1MINUTE
      - Else -> SECOND
      - If target is exactly HOUR -> HOUR
    """
    if supported_periods is None:
        # Default to IG-supported CHART scales
        supported_periods = ["SECOND", "1MINUTE", "5MINUTE", "HOUR"]

    tp = target_period.strip().upper()

    if tp == "HOUR" and "HOUR" in supported_periods:
        return "HOUR"

    target_s = period_to_seconds(tp)

    # Try 5MINUTE first if supported
    if "5MINUTE" in supported_periods:
        if target_s >= 300 and (target_s % 300 == 0):
            return "5MINUTE"

    # Try 1MINUTE if supported
    if "1MINUTE" in supported_periods:
        if target_s >= 60 and (target_s % 60 == 0):
            return "1MINUTE"

    # Fall back to SECOND if supported
    if "SECOND" in supported_periods:
        if target_s >= 1 and (target_s % 1 == 0):
            return "SECOND"

    raise ValueError(
        f"Cannot choose base period for target_period={target_period!r} "
        f"with supported_periods={supported_periods}"
    )


@dataclass
class _AggState:
    """Internal aggregation state for a single time bucket."""

    count: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_count: int
    last_ts: str


class CandleAggregator:
    """
    Aggregate base-period candles into a higher timeframe using wall-clock
    time bucketing (not count-based).

    - Buckets are aligned to target_period boundaries (UTC).
    - One instance can manage multiple instruments concurrently.
    - Missing base candles are tolerated.

    Usage:
        aggregator = CandleAggregator(target_period="15MINUTE")
        result = aggregator.update(instrument="EURUSD", candle=base_candle)
        # Returns aggregated Candle when bucket rolls, None while accumulating
    """

    def __init__(
        self,
        *,
        target_period: str,
        base_period: Optional[str] = None,
        supported_periods: list[str] | None = None,
    ):
        """
        Initialize aggregator.

        Args:
            target_period: Target aggregation period (e.g., "15MINUTE")
            base_period: Base candle period (auto-selected if None)
            supported_periods: Broker-supported periods for base_period selection
        """
        self.target_period = target_period.strip().upper()
        self.base_period = (
            (
                base_period
                or choose_base_period(self.target_period, supported_periods=supported_periods)
            )
            .strip()
            .upper()
        )

        self.target_s = period_to_seconds(self.target_period)
        self.base_s = period_to_seconds(self.base_period)

        if self.target_s % self.base_s != 0:
            raise ValueError(
                f"target_period ({self.target_period}) must be a multiple of "
                f"base_period ({self.base_period})"
            )

        # Per-instrument state: (bucket_start_ts, agg_state)
        self._state: dict[str, tuple[int, _AggState]] = {}

    def reset(self, instrument: str) -> None:
        """Reset aggregation state for an instrument."""
        self._state.pop(instrument, None)

    def update(self, *, instrument: str, candle: Candle) -> Optional[Candle]:
        """
        Update aggregator with a new base-period candle.

        Args:
            instrument: Instrument identifier
            candle: Base-period candle

        Returns:
            Aggregated candle when bucket rolls, None while accumulating
        """
        ts_ms = int(candle.timestamp)
        ts_s = ts_ms // 1000

        bucket_start = (ts_s // self.target_s) * self.target_s

        state = self._state.get(instrument)

        if state is None:
            # Start new bucket
            agg = _AggState(
                count=1,
                open=float(candle.open),
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=float(getattr(candle, "volume", 0.0) or 0.0),
                tick_count=int(getattr(candle, "tick_count", 0) or 0),
                last_ts=str(candle.timestamp),
            )
            self._state[instrument] = (bucket_start, agg)
            return None

        current_bucket_start, agg = state

        if bucket_start == current_bucket_start:
            # Accumulate into current bucket
            agg.count += 1
            agg.high = max(agg.high, float(candle.high))
            agg.low = min(agg.low, float(candle.low))
            agg.close = float(candle.close)
            agg.volume += float(getattr(candle, "volume", 0.0) or 0.0)
            agg.tick_count += int(getattr(candle, "tick_count", 0) or 0)
            agg.last_ts = str(candle.timestamp)
            return None

        # Bucket rolled -> emit previous aggregated candle
        prev_bucket_start = current_bucket_start
        prev_bucket_end = prev_bucket_start + self.target_s
        out = Candle(
            timestamp=str(prev_bucket_end * 1000),
            open=agg.open,
            high=agg.high,
            low=agg.low,
            close=agg.close,
            volume=agg.volume,
            tick_count=agg.tick_count,
        )

        # Start new bucket with current candle
        new_agg = _AggState(
            count=1,
            open=float(candle.open),
            high=float(candle.high),
            low=float(candle.low),
            close=float(candle.close),
            volume=float(getattr(candle, "volume", 0.0) or 0.0),
            tick_count=int(getattr(candle, "tick_count", 0) or 0),
            last_ts=str(candle.timestamp),
        )
        self._state[instrument] = (bucket_start, new_agg)

        return out

    def describe(self) -> tuple[str, str, int]:
        """Return (base_period, target_period, factor) for debugging."""
        factor = self.target_s // self.base_s
        return (self.base_period, self.target_period, factor)
