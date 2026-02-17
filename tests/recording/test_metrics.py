"""Tests for performance metrics computation."""

import pytest

from tradedesk import Direction
from tradedesk.recording.metrics import (
    compute_metrics,
    equity_rows_from_round_trips,
    max_drawdown,
    round_trips_from_fills,
)


def test_round_trips_long_single_instrument() -> None:
    """Test round trip reconstruction for a single long trade."""
    fills = [
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "100",
            "size": "2",
        },
        {
            "instrument": "EURUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:05:00Z",
            "price": "105",
            "size": "2",
        },
    ]

    trips = round_trips_from_fills(fills)
    assert len(trips) == 1
    t = trips[0]
    assert t.instrument == "EURUSD"
    assert t.direction == Direction.LONG
    assert t.pnl == pytest.approx((105 - 100) * 2)


def test_round_trips_short_single_instrument() -> None:
    """Test round trip reconstruction for a single short trade."""
    fills = [
        {
            "instrument": "GBPUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "200",
            "size": "1",
        },
        {
            "instrument": "GBPUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:03:00Z",
            "price": "180",
            "size": "1",
        },
    ]

    trips = round_trips_from_fills(fills)
    assert len(trips) == 1
    assert trips[0].direction == Direction.SHORT
    assert trips[0].pnl == pytest.approx((200 - 180) * 1)


def test_round_trips_multiple_instruments_interleaved() -> None:
    """Test round trip reconstruction with multiple instruments."""
    fills = [
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "10",
            "size": "1",
        },
        {
            "instrument": "GBPUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:01:00Z",
            "price": "50",
            "size": "2",
        },
        {
            "instrument": "EURUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:02:00Z",
            "price": "12",
            "size": "1",
        },
        {
            "instrument": "GBPUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:03:00Z",
            "price": "55",
            "size": "2",
        },
    ]

    trips = round_trips_from_fills(fills)
    assert len(trips) == 2

    t0, t1 = trips
    assert (t0.instrument, t0.direction, t0.pnl) == (
        "EURUSD",
        Direction.LONG,
        pytest.approx((12 - 10) * 1),
    )
    assert (t1.instrument, t1.direction, t1.pnl) == (
        "GBPUSD",
        Direction.SHORT,
        pytest.approx((50 - 55) * 2),
    )


def test_round_trips_supports_epic_field_for_backward_compatibility() -> None:
    """Test that round_trips_from_fills also accepts 'epic' field."""
    fills = [
        {
            "epic": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "100",
            "size": "1",
        },
        {
            "epic": "EURUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:05:00Z",
            "price": "105",
            "size": "1",
        },
    ]

    trips = round_trips_from_fills(fills)
    assert len(trips) == 1
    assert trips[0].instrument == "EURUSD"


def test_round_trips_size_mismatch_raises() -> None:
    """Test that size mismatch raises an error."""
    fills = [
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "100",
            "size": "1",
        },
        {
            "instrument": "EURUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:05:00Z",
            "price": "101",
            "size": "2",
        },
    ]

    with pytest.raises(ValueError, match="Size mismatch"):
        round_trips_from_fills(fills)


def test_round_trips_incomplete_open_position_is_ignored() -> None:
    """Test that incomplete positions are ignored."""
    fills = [
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "100",
            "size": "1",
        },
    ]

    trips = round_trips_from_fills(fills)
    assert len(trips) == 0


def test_equity_rows_from_round_trips_cumulative_pnl() -> None:
    """Test equity curve construction from round trips."""
    fills = [
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:00:00Z",
            "price": "10",
            "size": "1",
        },
        {
            "instrument": "EURUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:10:00Z",
            "price": "12",
            "size": "1",
        },
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2025-01-01T00:20:00Z",
            "price": "10",
            "size": "1",
        },
        {
            "instrument": "EURUSD",
            "direction": "SELL",
            "timestamp": "2025-01-01T00:30:00Z",
            "price": "15",
            "size": "1",
        },
    ]

    trips = round_trips_from_fills(fills)
    rows = equity_rows_from_round_trips(trips, starting_equity=100.0)

    assert len(rows) == 2
    assert float(rows[0]["equity"]) == pytest.approx(102.0)  # 100 + 2
    assert float(rows[1]["equity"]) == pytest.approx(107.0)  # 102 + 5


def test_max_drawdown_empty_equity_returns_zero() -> None:
    """Test max drawdown with empty equity list."""
    assert max_drawdown([]) == 0.0


def test_max_drawdown_monotonic_up_returns_zero() -> None:
    """Test max drawdown with monotonically increasing equity."""
    assert max_drawdown([100, 101, 102, 103]) == 0.0


def test_max_drawdown_simple_drop() -> None:
    """Test max drawdown with a simple drop."""
    dd = max_drawdown([100, 110, 105, 95])
    assert dd == pytest.approx(-15.0)  # peak 110, trough 95


def test_max_drawdown_multiple_peaks_and_troughs() -> None:
    """Test max drawdown with multiple peaks and troughs."""
    dd = max_drawdown([100, 110, 90, 105, 80, 120])
    assert dd == pytest.approx(-30.0)  # peak 110, trough 80


def test_compute_metrics_empty_inputs() -> None:
    """Test compute_metrics with empty inputs."""
    m = compute_metrics(equity_rows=[], trade_rows=[])

    assert m.trades == 0
    assert m.round_trips == 0
    assert m.final_equity == 0.0
    assert m.max_drawdown == 0.0


def test_compute_metrics_equity_filters_none_and_empty_string() -> None:
    """Test that compute_metrics filters out None and empty equity values."""
    equity_rows = [
        {"timestamp": "2025-01-01T00:00:00+00:00", "equity": "100"},
        {"timestamp": "2025-01-01T00:05:00+00:00", "equity": None},
        {"timestamp": "2025-01-01T00:10:00+00:00", "equity": ""},
        {"timestamp": "2025-01-01T00:15:00+00:00", "equity": "105"},
    ]
    trade_rows = []

    m = compute_metrics(equity_rows=equity_rows, trade_rows=trade_rows)
    assert m.final_equity == 105.0


def test_compute_metrics_wins_only_profit_factor_inf() -> None:
    """Test profit factor is inf when only wins."""
    equity_rows = [
        {"timestamp": "2025-01-01T00:00:00+00:00", "equity": "0"},
        {"timestamp": "2025-01-01T00:05:00+00:00", "equity": "10"},
    ]

    trade_rows = [
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "100",
        },
        {
            "timestamp": "2025-01-01T00:05:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "110",
        },
    ]

    m = compute_metrics(equity_rows=equity_rows, trade_rows=trade_rows)

    assert m.profit_factor == float("inf")
    assert m.win_rate == 1.0


def test_compute_metrics_losses_only_profit_factor_zero() -> None:
    """Test profit factor is 0 when only losses."""
    equity_rows = [
        {"timestamp": "2025-01-01T00:00:00+00:00", "equity": "0"},
        {"timestamp": "2025-01-01T00:05:00+00:00", "equity": "-10"},
    ]

    trade_rows = [
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "100",
        },
        {
            "timestamp": "2025-01-01T00:05:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "90",
        },
    ]

    m = compute_metrics(equity_rows=equity_rows, trade_rows=trade_rows)

    assert m.profit_factor == 0.0
    assert m.win_rate == 0.0


def test_compute_metrics_mixed_wins_and_losses() -> None:
    """Test compute_metrics with mixed wins and losses."""
    equity_rows = [
        {"timestamp": "2025-01-01T00:00:00+00:00", "equity": "0"},
        {"timestamp": "2025-01-01T00:05:00+00:00", "equity": "10"},
        {"timestamp": "2025-01-01T00:10:00+00:00", "equity": "5"},
    ]

    trade_rows = [
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "100",
        },
        {
            "timestamp": "2025-01-01T00:05:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "110",
        },
        {
            "timestamp": "2025-01-01T00:07:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "110",
        },
        {
            "timestamp": "2025-01-01T00:10:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "105",
        },
    ]

    m = compute_metrics(equity_rows=equity_rows, trade_rows=trade_rows)

    assert m.round_trips == 2
    assert m.wins == 1
    assert m.losses == 1
    assert m.win_rate == 0.5
    assert m.avg_win == pytest.approx(10.0)
    assert m.avg_loss == pytest.approx(-5.0)
    assert m.profit_factor == pytest.approx(2.0)


def test_compute_metrics_reporting_scale_scales_linear_outputs_only() -> None:
    """Test that reporting_scale only affects linear metrics."""
    equity_rows = [
        {"timestamp": "2025-01-01T00:00:00+00:00", "equity": "0"},
        {"timestamp": "2025-01-01T00:05:00+00:00", "equity": "10"},
    ]

    trade_rows = [
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "100",
        },
        {
            "timestamp": "2025-01-01T00:05:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "110",
        },
    ]

    m = compute_metrics(
        equity_rows=equity_rows, trade_rows=trade_rows, reporting_scale=2.0
    )

    # Scaled
    assert m.final_equity == pytest.approx(20.0)
    assert m.avg_win == pytest.approx(20.0)
    assert m.expectancy == pytest.approx(20.0)

    # Not scaled
    assert m.profit_factor == float("inf")
    assert m.win_rate == 1.0


def test_compute_metrics_counts_exits_by_reason() -> None:
    """Test that exit reasons are counted correctly."""
    equity_rows = [
        {"timestamp": "2025-01-01T00:00:00+00:00", "equity": "0"},
        {"timestamp": "2025-01-01T00:05:00+00:00", "equity": "10"},
        {"timestamp": "2025-01-01T00:10:00+00:00", "equity": "5"},
    ]

    trade_rows = [
        {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "100",
        },
        {
            "timestamp": "2025-01-01T00:05:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "110",
            "reason": "take_profit",
        },
        {
            "timestamp": "2025-01-01T00:07:00+00:00",
            "instrument": "EURUSD",
            "direction": "BUY",
            "size": "1",
            "price": "110",
        },
        {
            "timestamp": "2025-01-01T00:10:00+00:00",
            "instrument": "EURUSD",
            "direction": "SELL",
            "size": "1",
            "price": "105",
            "reason": "stop_loss",
        },
    ]

    m = compute_metrics(equity_rows=equity_rows, trade_rows=trade_rows)

    assert m.exits_by_reason["take_profit"] == 1
    assert m.exits_by_reason["stop_loss"] == 1
