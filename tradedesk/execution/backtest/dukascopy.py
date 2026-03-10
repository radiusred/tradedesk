"""Dukascopy cache reader for tradedesk backtests.

Reads daily 1-minute candle files from the Dukascopy cache directory layout::

    {cache_dir}/{SYMBOL}/{YEAR}/{MM}/{DD}_{side}.csv.zst

where MM is zero-based (Jan=00, Dec=11) and side is ``"bid"`` or ``"ask"``.

Files are Zstandard-compressed CSV with columns::

    timestamp,open,high,low,close,volume

Old-format hourly .bi5 files (in per-day subdirectories) are detected and
cause an immediate exit with a message to re-run ``tradedesk-dc-export``.
"""

from __future__ import annotations

import io
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import zstandard as zstd

from tradedesk.types import Candle

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _iter_days(date_from: date, date_to: date) -> list[date]:
    """Return all dates in [date_from, date_to] inclusive."""
    days: list[date] = []
    d = date_from
    while d <= date_to:
        days.append(d)
        d += timedelta(days=1)
    return days


def _candle_path(cache_dir: Path, symbol: str, day: date, side: str) -> Path:
    """Return the path to a daily 1-min candle cache file (Zstandard compressed)."""
    month_0 = f"{day.month - 1:02d}"
    return cache_dir / symbol / str(day.year) / month_0 / f"{day.day:02d}_{side}.csv.zst"


def _check_old_format(cache_dir: Path, symbol: str, day: date) -> None:
    """Exit with a helpful message if old-style hourly .bi5 files are found."""
    month_0 = f"{day.month - 1:02d}"
    day_dir = cache_dir / symbol / str(day.year) / month_0 / f"{day.day:02d}"
    if day_dir.is_dir() and any(day_dir.glob("*.bi5")):
        sys.exit(
            f"Old Dukascopy cache format detected in {day_dir}.\n"
            "Please re-run 'tradedesk-dc-export' to update the cache to the "
            "daily candle file format."
        )


def _load_daily_candles(path: Path) -> pd.DataFrame | None:
    """
    Load a Zstandard-compressed 1-min candle CSV.

    Returns ``None`` if the file does not exist or cannot be parsed.
    The returned DataFrame has a UTC DatetimeIndex named ``"timestamp"``.
    """
    if not path.exists():
        return None
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f_in:
            with dctx.stream_reader(f_in) as reader:
                df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")
    except Exception:
        return None


def _period_to_pandas_rule(period: str) -> str:
    """
    Convert a tradedesk period string to a pandas resample rule.

    Examples::

        "1MIN"     -> "1min"
        "15MIN"    -> "15min"
        "5MINUTE"  -> "5min"
        "1H"       -> "1h"
        "4H"       -> "4h"
        "1D"       -> "1D"
    """
    p = period.strip().upper()
    m = re.fullmatch(r"(\d+)(MIN(?:UTE)?|H(?:OUR)?|D(?:AY)?|W(?:EEK)?)", p)
    if not m:
        raise ValueError(
            f"Cannot convert period {period!r} to a pandas resample rule. "
            "Expected format like '1MIN', '15MIN', '1H', '4H', '1D'."
        )
    n, unit = m.group(1), m.group(2)
    if unit.startswith("MIN"):
        return f"{n}min"
    if unit.startswith("H"):
        return f"{n}h"
    if unit.startswith("D"):
        return f"{n}D"
    if unit.startswith("W"):
        return f"{n}W"
    raise ValueError(f"Unsupported period unit in: {period!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_dukascopy_candles(
    cache_dir: Path,
    symbol: str,
    period: str,
    date_from: date,
    date_to: date,
    *,
    price_side: str = "bid",
) -> list[Candle]:
    """
    Read 1-minute candle data from a Dukascopy cache and resample to OHLCV ``Candle`` objects.

    Args:
        cache_dir: Root of the Dukascopy cache directory.
        symbol: Symbol subfolder name (e.g. ``"EURUSD"``).
        period: Tradedesk period string (e.g. ``"1MIN"``, ``"15MIN"``, ``"1H"``).
        date_from: First date to include (inclusive, UTC).
        date_to: Last date to include (inclusive, UTC).
        price_side: ``"bid"`` (default) or ``"ask"``.

    Returns:
        List of ``Candle`` objects ordered oldest to newest.

    Raises:
        SystemExit: If old-format ``.bi5`` hourly files are found in the cache.
        ValueError: If ``price_side`` is not ``"bid"`` or ``"ask"``.
        ValueError: If no candle data is found in the requested date range.
    """
    if price_side not in ("bid", "ask"):
        raise ValueError(f"price_side must be 'bid' or 'ask'; got {price_side!r}")

    resample_rule = _period_to_pandas_rule(period)
    frames: list[pd.DataFrame] = []

    for day in _iter_days(date_from, date_to):
        _check_old_format(cache_dir, symbol, day)
        path = _candle_path(cache_dir, symbol, day, price_side)
        df = _load_daily_candles(path)
        if df is None or df.empty:
            continue
        frames.append(df)

    if not frames:
        raise ValueError(
            f"No candle data found for symbol={symbol!r} in cache {cache_dir} "
            f"between {date_from} and {date_to}."
        )

    all_1min = pd.concat(frames).sort_index()
    resampled = all_1min.resample(resample_rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    resampled = resampled.dropna(subset=["open"])

    candles: list[Candle] = []
    for ts, row in resampled.iterrows():
        ts_str = pd.Timestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
        candles.append(
            Candle(
                timestamp=ts_str,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
        )

    return candles
