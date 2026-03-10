"""Tests for tradedesk.execution.backtest.dukascopy."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest
import zstandard as zstd

from tradedesk.execution.backtest.client import BacktestClient
from tradedesk.execution.backtest.dukascopy import (
    _candle_path,
    _check_old_format,
    _iter_days,
    _load_daily_candles,
    _period_to_pandas_rule,
    read_dukascopy_candles,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_candle_csv(rows: list[tuple[str, float, float, float, float, float]]) -> str:
    lines = ["timestamp,open,high,low,close,volume"]
    for ts, o, h, l, c, v in rows:
        lines.append(f"{ts},{o},{h},{l},{c},{v}")
    return "\n".join(lines) + "\n"


def _write_candle_file(
    path: Path, rows: list[tuple[str, float, float, float, float, float]]
) -> None:
    """Write a compressed daily candle file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    content = _make_candle_csv(rows).encode("utf-8")
    cctx = zstd.ZstdCompressor(level=3)
    path.write_bytes(cctx.compress(content))


def _sample_candle_rows(day: date) -> list[tuple[str, float, float, float, float, float]]:
    """Two 1-min candles for a given day."""
    return [
        (f"{day.isoformat()} 00:00:00+00:00", 1.1000, 1.1005, 1.0998, 1.1001, 1.7),
        (f"{day.isoformat()} 00:01:00+00:00", 1.1001, 1.1008, 1.1000, 1.1005, 2.3),
    ]


# ---------------------------------------------------------------------------
# _iter_days
# ---------------------------------------------------------------------------


def test_iter_days_single() -> None:
    assert _iter_days(date(2025, 1, 5), date(2025, 1, 5)) == [date(2025, 1, 5)]


def test_iter_days_range() -> None:
    result = _iter_days(date(2025, 1, 1), date(2025, 1, 3))
    assert result == [date(2025, 1, 1), date(2025, 1, 2), date(2025, 1, 3)]


def test_iter_days_month_boundary() -> None:
    result = _iter_days(date(2025, 1, 30), date(2025, 2, 2))
    assert result == [
        date(2025, 1, 30),
        date(2025, 1, 31),
        date(2025, 2, 1),
        date(2025, 2, 2),
    ]


# ---------------------------------------------------------------------------
# _candle_path
# ---------------------------------------------------------------------------


def test_candle_path_january() -> None:
    cache = Path("/cache")
    assert _candle_path(cache, "EURUSD", date(2025, 1, 5), "bid") == Path(
        "/cache/EURUSD/2025/00/05_bid.csv.zst"
    )
    assert _candle_path(cache, "EURUSD", date(2025, 1, 5), "ask") == Path(
        "/cache/EURUSD/2025/00/05_ask.csv.zst"
    )


def test_candle_path_december() -> None:
    cache = Path("/cache")
    assert _candle_path(cache, "USDJPY", date(2025, 12, 31), "bid") == Path(
        "/cache/USDJPY/2025/11/31_bid.csv.zst"
    )


def test_candle_path_year_boundary() -> None:
    cache = Path("/cache")
    assert _candle_path(cache, "GBPUSD", date(2026, 1, 1), "ask") == Path(
        "/cache/GBPUSD/2026/00/01_ask.csv.zst"
    )


# ---------------------------------------------------------------------------
# _period_to_pandas_rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "period,expected",
    [
        ("1MIN", "1min"),
        ("15MIN", "15min"),
        ("5MINUTE", "5min"),
        ("1MINUTE", "1min"),
        ("1H", "1h"),
        ("4H", "4h"),
        ("1HOUR", "1h"),
        ("1D", "1D"),
        ("1DAY", "1D"),
        ("1W", "1W"),
        ("1WEEK", "1W"),
    ],
)
def test_period_to_pandas_rule(period: str, expected: str) -> None:
    assert _period_to_pandas_rule(period) == expected


def test_period_to_pandas_rule_invalid() -> None:
    with pytest.raises(ValueError, match="Cannot convert period"):
        _period_to_pandas_rule("UNKNOWN")


# ---------------------------------------------------------------------------
# _check_old_format
# ---------------------------------------------------------------------------


def test_check_old_format_no_directory(tmp_path: Path) -> None:
    """Missing directory is fine — no bi5 files possible."""
    _check_old_format(tmp_path, "EURUSD", date(2025, 1, 5))


def test_check_old_format_no_bi5(tmp_path: Path) -> None:
    day_dir = tmp_path / "EURUSD" / "2025" / "00" / "05"
    day_dir.mkdir(parents=True)
    (day_dir / "some_other_file.txt").touch()
    _check_old_format(tmp_path, "EURUSD", date(2025, 1, 5))


def test_check_old_format_with_bi5_exits(tmp_path: Path) -> None:
    day_dir = tmp_path / "EURUSD" / "2025" / "00" / "05"
    day_dir.mkdir(parents=True)
    (day_dir / "00h_ticks.bi5").touch()
    with pytest.raises(SystemExit):
        _check_old_format(tmp_path, "EURUSD", date(2025, 1, 5))


# ---------------------------------------------------------------------------
# _load_daily_candles
# ---------------------------------------------------------------------------


def test_load_daily_candles_missing_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "missing.csv.zst"
    assert _load_daily_candles(path) is None


def test_load_daily_candles_compressed(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    path = _candle_path(tmp_path, "EURUSD", day, "bid")
    _write_candle_file(path, _sample_candle_rows(day))

    df = _load_daily_candles(path)
    assert df is not None
    assert not df.empty
    assert len(df) == 2
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df.index.tz is not None  # UTC-aware


def test_load_daily_candles_ohlcv_values(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    path = _candle_path(tmp_path, "EURUSD", day, "ask")
    rows = _sample_candle_rows(day)
    _write_candle_file(path, rows)

    df = _load_daily_candles(path)
    assert df is not None
    assert abs(float(df.iloc[0]["open"]) - 1.1000) < 1e-9
    assert abs(float(df.iloc[0]["high"]) - 1.1005) < 1e-9


def test_load_daily_candles_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv.zst"
    path.write_bytes(b"not valid zstd data")
    assert _load_daily_candles(path) is None


# ---------------------------------------------------------------------------
# read_dukascopy_candles
# ---------------------------------------------------------------------------


def test_read_dukascopy_candles_single_day(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    path = _candle_path(tmp_path, "EURUSD", day, "bid")
    _write_candle_file(path, _sample_candle_rows(day))

    candles = read_dukascopy_candles(tmp_path, "EURUSD", "1MIN", day, day)
    assert len(candles) == 2
    assert candles[0].timestamp.endswith("Z")
    assert candles[0].open > 0
    assert candles[0].high >= candles[0].open
    assert candles[0].low <= candles[0].open


def test_read_dukascopy_candles_ask_side(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    bid_path = _candle_path(tmp_path, "EURUSD", day, "bid")
    ask_path = _candle_path(tmp_path, "EURUSD", day, "ask")
    # Write different values for bid vs ask so we can tell which was loaded
    bid_rows = _sample_candle_rows(day)
    ask_rows = [
        (ts, o + 0.0002, h + 0.0002, lo + 0.0002, c + 0.0002, v) for ts, o, h, lo, c, v in bid_rows
    ]
    _write_candle_file(bid_path, bid_rows)
    _write_candle_file(ask_path, ask_rows)

    candles = read_dukascopy_candles(tmp_path, "EURUSD", "1MIN", day, day, price_side="ask")
    assert abs(candles[0].open - 1.1002) < 1e-9


def test_read_dukascopy_candles_multi_day(tmp_path: Path) -> None:
    for d in [date(2025, 6, 15), date(2025, 6, 16), date(2025, 6, 17)]:
        path = _candle_path(tmp_path, "EURUSD", d, "bid")
        _write_candle_file(path, _sample_candle_rows(d))

    candles = read_dukascopy_candles(
        tmp_path, "EURUSD", "1MIN", date(2025, 6, 15), date(2025, 6, 17)
    )
    assert len(candles) == 6  # 2 candles × 3 days


def test_read_dukascopy_candles_missing_day_skipped(tmp_path: Path) -> None:
    """A missing day (market closed) is silently skipped."""
    day1 = date(2025, 6, 15)
    day3 = date(2025, 6, 17)
    for d in [day1, day3]:
        path = _candle_path(tmp_path, "EURUSD", d, "bid")
        _write_candle_file(path, _sample_candle_rows(d))

    candles = read_dukascopy_candles(tmp_path, "EURUSD", "1MIN", day1, day3)
    assert len(candles) == 4  # 2 candles × 2 days


def test_read_dukascopy_candles_no_data_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No candle data found"):
        read_dukascopy_candles(tmp_path, "EURUSD", "1MIN", date(2025, 6, 15), date(2025, 6, 15))


def test_read_dukascopy_candles_invalid_side_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="price_side"):
        read_dukascopy_candles(
            tmp_path,
            "EURUSD",
            "1MIN",
            date(2025, 6, 15),
            date(2025, 6, 15),
            price_side="mid",
        )


def test_read_dukascopy_candles_old_format_exits(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    day_dir = tmp_path / "EURUSD" / "2025" / "05" / "15"
    day_dir.mkdir(parents=True)
    (day_dir / "00h_ticks.bi5").touch()

    with pytest.raises(SystemExit):
        read_dukascopy_candles(tmp_path, "EURUSD", "1MIN", day, day)


def test_read_dukascopy_candles_timestamps_ordered(tmp_path: Path) -> None:
    for d in [date(2025, 6, 15), date(2025, 6, 16)]:
        path = _candle_path(tmp_path, "EURUSD", d, "bid")
        _write_candle_file(path, _sample_candle_rows(d))

    candles = read_dukascopy_candles(
        tmp_path, "EURUSD", "1MIN", date(2025, 6, 15), date(2025, 6, 16)
    )
    timestamps = [c.timestamp for c in candles]
    assert timestamps == sorted(timestamps)


def test_read_dukascopy_candles_aggregates_to_15min(tmp_path: Path) -> None:
    """A day with 30 1-min candles resampled to 15MIN yields 2 candles."""
    day = date(2025, 6, 15)
    rows = [
        (
            f"{day.isoformat()} {h:02d}:{m:02d}:00+00:00",
            1.1000 + m * 0.0001,
            1.1010 + m * 0.0001,
            1.0990 + m * 0.0001,
            1.1005 + m * 0.0001,
            1.0,
        )
        for h in [0]
        for m in range(30)
    ]
    path = _candle_path(tmp_path, "EURUSD", day, "bid")
    _write_candle_file(path, rows)

    candles = read_dukascopy_candles(tmp_path, "EURUSD", "15MIN", day, day)
    assert len(candles) == 2


# ---------------------------------------------------------------------------
# BacktestClient.from_dukascopy_cache integration
# ---------------------------------------------------------------------------


def test_backtest_client_from_dukascopy_cache(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    path = _candle_path(tmp_path, "EURUSD", day, "bid")
    _write_candle_file(path, _sample_candle_rows(day))

    client = BacktestClient.from_dukascopy_cache(
        tmp_path,
        symbol="EURUSD",
        instrument="CS.D.EURUSD.TODAY.IP",
        period="1MIN",
        date_from=day,
        date_to=day,
    )
    candles = client._history[("CS.D.EURUSD.TODAY.IP", "1MIN")]
    assert len(candles) == 2


def test_backtest_client_from_dukascopy_cache_ask_side(tmp_path: Path) -> None:
    day = date(2025, 6, 15)
    ask_path = _candle_path(tmp_path, "EURUSD", day, "ask")
    ask_rows = [
        (ts, o + 0.0002, h + 0.0002, lo + 0.0002, c + 0.0002, v)
        for ts, o, h, lo, c, v in _sample_candle_rows(day)
    ]
    _write_candle_file(ask_path, ask_rows)

    client = BacktestClient.from_dukascopy_cache(
        tmp_path,
        symbol="EURUSD",
        instrument="CS.D.EURUSD.TODAY.IP",
        period="1MIN",
        date_from=day,
        date_to=day,
        price_side="ask",
    )
    candles = client._history[("CS.D.EURUSD.TODAY.IP", "1MIN")]
    assert len(candles) == 2
    assert abs(candles[0].open - 1.1002) < 1e-9
