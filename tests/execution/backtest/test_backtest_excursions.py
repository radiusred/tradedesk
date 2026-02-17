"""Tests for tradedesk.execution.backtest.excursions – MFE/MAE computation."""

import pytest

from tradedesk import Direction
from tradedesk.execution.backtest.excursions import (
    Excursions,
    build_candle_index,
    compute_excursions,
)
from tradedesk.recording.metrics import RoundTrip
from tradedesk.types import Candle


def _candle(ts: str, high: float, low: float) -> Candle:
    return Candle(timestamp=ts, open=low, high=high, low=low, close=high)


class TestBuildCandleIndex:
    def test_basic(self):
        candles = [
            _candle("2025-01-01T00:00:00Z", 105.0, 95.0),
            _candle("2025-01-01T01:00:00Z", 110.0, 99.0),
        ]
        idx = build_candle_index(candles)
        assert len(idx.ts) == 2
        assert len(idx.high) == 2
        assert len(idx.low) == 2

    def test_sorts_by_timestamp(self):
        candles = [
            _candle("2025-01-01T02:00:00Z", 110.0, 99.0),
            _candle("2025-01-01T01:00:00Z", 105.0, 95.0),
        ]
        idx = build_candle_index(candles)
        assert idx.ts[0] < idx.ts[1]
        assert idx.high[0] == 105.0  # first after sort
        assert idx.high[1] == 110.0

    def test_empty(self):
        idx = build_candle_index([])
        assert len(idx.ts) == 0


class TestComputeExcursions:
    def _make_index(self):
        candles = [
            _candle("2025-01-01T00:00:00Z", 105.0, 95.0),
            _candle("2025-01-01T01:00:00Z", 110.0, 90.0),
            _candle("2025-01-01T02:00:00Z", 108.0, 92.0),
        ]
        return build_candle_index(candles)

    def test_long_excursions(self):
        idx = self._make_index()
        trip = RoundTrip(
            instrument="USDJPY",
            direction=Direction.LONG,
            entry_ts="2025-01-01T00:00:00Z",
            exit_ts="2025-01-01T02:00:00Z",
            entry_price=100.0,
            exit_price=105.0,
            size=1.0,
            pnl=5.0,
        )
        exc = compute_excursions(trip=trip, idx=idx)
        # MFE: max_high(105, 110, 108) - entry_price(100) = 10
        assert exc.mfe_points == pytest.approx(10.0)
        # MAE: min_low(95, 90, 92) - entry_price(100) = -10
        assert exc.mae_points == pytest.approx(-10.0)
        # PnL units
        assert exc.mfe_pnl == pytest.approx(10.0)
        assert exc.mae_pnl == pytest.approx(-10.0)

    def test_short_excursions(self):
        idx = self._make_index()
        trip = RoundTrip(
            instrument="USDJPY",
            direction=Direction.SHORT,
            entry_ts="2025-01-01T00:00:00Z",
            exit_ts="2025-01-01T02:00:00Z",
            entry_price=100.0,
            exit_price=95.0,
            size=2.0,
            pnl=10.0,
        )
        exc = compute_excursions(trip=trip, idx=idx)
        # MFE for short: entry_price(100) - min_low(90) = 10
        assert exc.mfe_points == pytest.approx(10.0)
        # MAE for short: entry_price(100) - max_high(110) = -10
        assert exc.mae_points == pytest.approx(-10.0)
        # PnL units: points * size
        assert exc.mfe_pnl == pytest.approx(20.0)
        assert exc.mae_pnl == pytest.approx(-20.0)

    def test_no_candle_coverage(self):
        idx = self._make_index()
        trip = RoundTrip(
            instrument="USDJPY",
            direction=Direction.LONG,
            entry_ts="2030-01-01T00:00:00Z",
            exit_ts="2030-01-01T01:00:00Z",
            entry_price=100.0,
            exit_price=105.0,
            size=1.0,
            pnl=5.0,
        )
        exc = compute_excursions(trip=trip, idx=idx)
        assert exc == Excursions(0.0, 0.0, 0.0, 0.0)
