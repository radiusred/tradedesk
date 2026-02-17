import pytest

from tradedesk import Direction
from tradedesk.execution.position import PositionTracker
from tradedesk.types import Candle


@pytest.fixture
def tracker():
    return PositionTracker()


def test_initial_state(tracker):
    assert tracker.direction is None
    assert tracker.size is None
    assert tracker.entry_price is None
    assert tracker.bars_held == 0
    assert tracker.mfe_points == 0.0
    assert tracker.is_flat() is True


def test_open_long_position(tracker):
    tracker.open(Direction.LONG, size=1.0, entry_price=100.0)

    assert tracker.direction == Direction.LONG
    assert tracker.size == 1.0
    assert tracker.entry_price == 100.0
    assert tracker.bars_held == 0
    assert tracker.mfe_points == 0.0
    assert tracker.is_flat() is False


def test_open_short_position(tracker):
    tracker.open(Direction.SHORT, size=2.5, entry_price=50.0)

    assert tracker.direction == Direction.SHORT
    assert tracker.size == 2.5
    assert tracker.entry_price == 50.0
    assert tracker.is_flat() is False


def test_reset(tracker):
    tracker.open(Direction.LONG, 1.0, 100.0)
    # Simulate some state changes
    tracker.bars_held = 5
    tracker.mfe_points = 10.0

    tracker.reset()

    assert tracker.direction is None
    assert tracker.size is None
    assert tracker.entry_price is None
    assert tracker.bars_held == 0
    assert tracker.mfe_points == 0.0
    assert tracker.is_flat() is True


def test_update_mfe_long(tracker):
    tracker.open(Direction.LONG, 1.0, 100.0)

    # High 110, Low 90. Entry 100. MFE = 110 - 100 = 10.
    c1 = Candle(timestamp="2023-01-01T12:00:00Z", open=100, high=110, low=90, close=105)
    tracker.update_mfe(c1)
    assert tracker.mfe_points == 10.0

    # High 105, Low 95. MFE should remain 10 (105-100 < 10).
    c2 = Candle(timestamp="2023-01-01T12:05:00Z", open=100, high=105, low=95, close=100)
    tracker.update_mfe(c2)
    assert tracker.mfe_points == 10.0

    # High 120. MFE should update to 20 (120-100).
    c3 = Candle(timestamp="2023-01-01T12:10:00Z", open=100, high=120, low=95, close=115)
    tracker.update_mfe(c3)
    assert tracker.mfe_points == 20.0


def test_update_mfe_short(tracker):
    tracker.open(Direction.SHORT, 1.0, 100.0)

    # High 110, Low 90. Entry 100. MFE = 100 - 90 = 10.
    c1 = Candle(timestamp="2023-01-01T12:00:00Z", open=100, high=110, low=90, close=95)
    tracker.update_mfe(c1)
    assert tracker.mfe_points == 10.0

    # High 105, Low 80. MFE = 100 - 80 = 20.
    c2 = Candle(timestamp="2023-01-01T12:05:00Z", open=95, high=105, low=80, close=85)
    tracker.update_mfe(c2)
    assert tracker.mfe_points == 20.0


def test_current_pnl_points(tracker):
    # Long
    tracker.open(Direction.LONG, 1.0, 100.0)
    assert tracker.current_pnl_points(105.0) == 5.0
    assert tracker.current_pnl_points(95.0) == -5.0

    # Short
    tracker.open(Direction.SHORT, 1.0, 100.0)
    assert tracker.current_pnl_points(90.0) == 10.0
    assert tracker.current_pnl_points(110.0) == -10.0
