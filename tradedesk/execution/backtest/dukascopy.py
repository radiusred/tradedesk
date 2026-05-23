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
import logging
import re
import sys
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import zstandard as zstd

from tradedesk.types import Candle

log = logging.getLogger(__name__)

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
    Load a 1-min candle CSV.

    Checks for a pre-decompressed ``.csv`` file first (ramdisk fast path); falls
    back to the Zstandard-compressed ``.csv.zst`` file at *path*.

    Returns ``None`` if neither file exists or if parsing fails.
    The returned DataFrame has a UTC DatetimeIndex named ``"timestamp"``.
    """
    csv_path = path.with_suffix("")  # {name}.csv.zst -> {name}.csv
    try:
        if csv_path.exists():
            df = pd.read_csv(csv_path)
        elif path.exists():
            dctx = zstd.ZstdDecompressor()
            with open(path, "rb") as f_in:
                with dctx.stream_reader(f_in) as reader:
                    df = pd.read_csv(io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"))
        else:
            return None
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")
    except (
        OSError,
        UnicodeDecodeError,
        zstd.ZstdError,
        pd.errors.EmptyDataError,
        pd.errors.ParserError,
        KeyError,
        ValueError,
    ) as e:
        # Surface skipped days so RAD-1920-style cache corruption is visible
        # to operators instead of being silently dropped from backtests.
        log.warning(
            "Skipping unreadable Dukascopy cache file %s: %s: %s",
            path,
            type(e).__name__,
            e,
        )
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
# Internal helpers (continued)
# ---------------------------------------------------------------------------


def _df_to_candles(df: "pd.DataFrame") -> list[Candle]:
    """Vectorised conversion of a OHLCV DataFrame to a list of Candle objects.

    Avoids ``iterrows()`` (which creates a Series per row) by extracting columns
    as Python lists in one vectorised call, then zipping.
    """
    timestamps = cast(pd.DatetimeIndex, df.index).strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
    opens = df["open"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    closes = df["close"].tolist()
    volumes = df["volume"].tolist()
    return [
        Candle(timestamp=ts, open=o, high=h, low=lo, close=c, volume=v)
        for ts, o, h, lo, c, v in zip(timestamps, opens, highs, lows, closes, volumes)
    ]


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

    return _df_to_candles(resampled)


def iter_dukascopy_candles(
    cache_dir: Path,
    symbol: str,
    period: str,
    date_from: date,
    date_to: date,
    *,
    price_side: str = "bid",
) -> Iterator[Candle]:
    """Lazily iterate OHLCV Candle objects from a Dukascopy cache, one day at a time.

    Yields candles in chronological order without loading the full date range into
    memory.  Each daily file is read, optionally resampled to *period*, and its
    candles are yielded before the next day is loaded.

    Unlike :func:`read_dukascopy_candles`, this function does **not** raise
    ``ValueError`` when no data is found — an empty date range simply produces no
    candles.  Input validation (``price_side``, ``period`` format) is performed
    eagerly at call time, before any iteration occurs.

    Args:
        cache_dir: Root of the Dukascopy cache directory.
        symbol: Symbol subfolder name (e.g. ``"EURUSD"``).
        period: Tradedesk period string (e.g. ``"1MIN"``, ``"15MIN"``, ``"1H"``).
        date_from: First date to include (inclusive, UTC).
        date_to: Last date to include (inclusive, UTC).
        price_side: ``"bid"`` (default) or ``"ask"``.

    Raises:
        ValueError: If ``price_side`` is not ``"bid"`` or ``"ask"``.
        ValueError: If *period* cannot be parsed (raised immediately, before iteration).
        SystemExit: If old-format ``.bi5`` hourly files are found in the cache.
    """
    # Validate eagerly before returning the generator — generator function bodies
    # only execute on first next() call, so we perform all input validation here
    # and delegate to a private generator.
    if price_side not in ("bid", "ask"):
        raise ValueError(f"price_side must be 'bid' or 'ask'; got {price_side!r}")
    resample_rule = _period_to_pandas_rule(period)  # raises ValueError for bad period
    return _iter_candles(cache_dir, symbol, resample_rule, date_from, date_to, price_side)


def _iter_candles(
    cache_dir: Path,
    symbol: str,
    resample_rule: str,
    date_from: date,
    date_to: date,
    price_side: str,
) -> Iterator[Candle]:
    """Private generator — assumes all inputs are pre-validated."""
    is_1min = resample_rule == "1min"

    for day in _iter_days(date_from, date_to):
        _check_old_format(cache_dir, symbol, day)
        path = _candle_path(cache_dir, symbol, day, price_side)
        df = _load_daily_candles(path)
        if df is None or df.empty:
            continue

        if is_1min:
            yield from _df_to_candles(df)
        else:
            resampled = df.resample(resample_rule).agg(
                {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
            )
            resampled = resampled.dropna(subset=["open"])
            yield from _df_to_candles(resampled)
