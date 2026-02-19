"""MFE/MAE (Maximum Favorable/Adverse Excursion) computation."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from tradedesk import Candle
from tradedesk.recording.metrics import RoundTrip
from tradedesk.time_utils import parse_timestamp


@dataclass(frozen=True)
class CandleIndex:
    ts: list[datetime]  # sorted ascending
    high: list[float]
    low: list[float]


def build_candle_index(candles: Iterable[Candle]) -> CandleIndex:
    ts: list[datetime] = []
    high: list[float] = []
    low: list[float] = []

    for c in candles:
        ts.append(parse_timestamp(c.timestamp))
        high.append(float(c.high))
        low.append(float(c.low))

    # Defensive: ensure sorted by timestamp (should already be)
    if any(ts[i] > ts[i + 1] for i in range(len(ts) - 1)):
        order = sorted(range(len(ts)), key=lambda i: ts[i])
        ts = [ts[i] for i in order]
        high = [high[i] for i in order]
        low = [low[i] for i in order]

    return CandleIndex(ts=ts, high=high, low=low)


@dataclass(frozen=True)
class Excursions:
    mfe_points: float
    mae_points: float
    mfe_pnl: float
    mae_pnl: float


def compute_excursions(*, trip: RoundTrip, idx: CandleIndex) -> Excursions:
    """
    Compute MFE/MAE using OHLC extremes between entry_ts and exit_ts (inclusive),
    using bisect slicing on a pre-built CandleIndex.

    Notes:
      - Uses candle high/low; intra-bar sequencing is unknown (standard limitation).
      - Returns excursions in both points and PnL units (points * size).
    """
    entry_ts = parse_timestamp(trip.entry_ts)
    exit_ts = parse_timestamp(trip.exit_ts)

    entry_price = float(trip.entry_price)
    size = float(trip.size)

    i = bisect_left(idx.ts, entry_ts)
    j = bisect_right(idx.ts, exit_ts)

    if i >= j:
        # No candle coverage in-window (alignment issue); return neutral excursions
        return Excursions(0.0, 0.0, 0.0, 0.0)

    max_high = max(idx.high[i:j])
    min_low = min(idx.low[i:j])

    if trip.direction == "LONG":
        mfe_points = max_high - entry_price
        mae_points = min_low - entry_price  # negative if adverse
    else:  # SHORT
        mfe_points = entry_price - min_low
        mae_points = entry_price - max_high  # negative if adverse

    return Excursions(
        mfe_points=float(mfe_points),
        mae_points=float(mae_points),
        mfe_pnl=float(mfe_points * size),
        mae_pnl=float(mae_points * size),
    )
