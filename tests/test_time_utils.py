"""Tests for tradedesk.time_utils – centralised timestamp handling."""

from datetime import datetime, timezone

from tradedesk.time_utils import (
    iso_to_ms,
    ms_to_iso,
    now_utc_iso,
    parse_timestamp,
)


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------

class TestParseTimestamp:

    def test_iso_string_with_z(self):
        dt = parse_timestamp("2025-01-15T12:30:00Z")
        assert dt == datetime(2025, 1, 15, 12, 30, tzinfo=timezone.utc)

    def test_iso_string_with_offset(self):
        dt = parse_timestamp("2025-01-15T12:30:00+00:00")
        assert dt == datetime(2025, 1, 15, 12, 30, tzinfo=timezone.utc)

    def test_iso_string_space_separator(self):
        dt = parse_timestamp("2025-01-15 12:30:00+00:00")
        assert dt == datetime(2025, 1, 15, 12, 30, tzinfo=timezone.utc)

    def test_slash_date_format(self):
        dt = parse_timestamp("2025/01/15T12:30:00Z")
        assert dt == datetime(2025, 1, 15, 12, 30, tzinfo=timezone.utc)

    def test_integer_milliseconds(self):
        # 2025-01-15T00:00:00Z = 1736899200000 ms
        ms = 1736899200000
        dt = parse_timestamp(ms)
        assert dt.year == 2025
        assert dt.month == 1
        assert dt.day == 15
        assert dt.tzinfo == timezone.utc

    def test_float_milliseconds(self):
        ms = 1736899200000.0
        dt = parse_timestamp(ms)
        assert dt.tzinfo == timezone.utc
        assert dt.year == 2025

    def test_numeric_string(self):
        dt = parse_timestamp("1736899200000")
        assert dt.year == 2025
        assert dt.tzinfo == timezone.utc

    def test_empty_string_returns_now(self):
        dt = parse_timestamp("")
        assert dt.tzinfo == timezone.utc
        # Should be very close to now
        diff = abs((datetime.now(timezone.utc) - dt).total_seconds())
        assert diff < 2.0

    def test_whitespace_only_returns_now(self):
        dt = parse_timestamp("   ")
        assert dt.tzinfo == timezone.utc

    def test_naive_iso_gets_utc(self):
        dt = parse_timestamp("2025-01-15T12:30:00")
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 12

    def test_negative_numeric_string(self):
        # Edge case: negative numeric string (before epoch)
        dt = parse_timestamp("-1000")
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# iso_to_ms / ms_to_iso
# ---------------------------------------------------------------------------

class TestIsoToMs:

    def test_round_trip(self):
        ts = "2025-01-15T12:30:00Z"
        ms = iso_to_ms(ts)
        assert isinstance(ms, int)
        iso_back = ms_to_iso(ms)
        # ms_to_iso uses space separator
        assert "2025-01-15" in iso_back
        assert "12:30:00" in iso_back

    def test_ms_to_iso_format(self):
        ms = 1736899200000
        result = ms_to_iso(ms)
        # Should have space separator, not T
        assert "T" not in result
        assert " " in result


class TestNowUtcIso:

    def test_returns_iso_string(self):
        result = now_utc_iso()
        # Should be parseable
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None
