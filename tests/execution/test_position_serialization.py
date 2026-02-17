"""Tests for PositionTracker.to_dict / from_dict serialization."""

import pytest

from tradedesk import Direction
from tradedesk.execution.position import PositionTracker


def test_flat_to_dict():
    t = PositionTracker()
    d = t.to_dict()
    assert d == {
        "direction": None,
        "size": None,
        "entry_price": None,
        "bars_held": 0,
        "mfe_points": 0.0,
    }


def test_flat_round_trip():
    t = PositionTracker()
    d = t.to_dict()
    t2 = PositionTracker.from_dict(d)
    assert t2.is_flat()
    assert t2.direction is None
    assert t2.size is None
    assert t2.entry_price is None
    assert t2.bars_held == 0
    assert t2.mfe_points == 0.0


def test_open_long_round_trip():
    t = PositionTracker()
    t.open(Direction.LONG, 1.5, 100.0)
    t.bars_held = 3
    t.mfe_points = 2.5

    d = t.to_dict()
    assert d["direction"] == "long"
    assert d["size"] == 1.5
    assert d["entry_price"] == 100.0

    t2 = PositionTracker.from_dict(d)
    assert t2.direction == Direction.LONG
    assert t2.size == 1.5
    assert t2.entry_price == 100.0
    assert t2.bars_held == 3
    assert t2.mfe_points == 2.5
    assert not t2.is_flat()


def test_open_short_round_trip():
    t = PositionTracker()
    t.open(Direction.SHORT, 0.5, 50.0)
    t.bars_held = 7
    t.mfe_points = 4.2

    t2 = PositionTracker.from_dict(t.to_dict())
    assert t2.direction == Direction.SHORT
    assert t2.size == 0.5
    assert t2.entry_price == 50.0
    assert t2.bars_held == 7
    assert t2.mfe_points == pytest.approx(4.2)


def test_from_dict_with_missing_optional_fields():
    """from_dict should handle missing bars_held/mfe_points gracefully."""
    data = {"direction": "long", "size": 1.0, "entry_price": 100.0}
    t = PositionTracker.from_dict(data)
    assert t.direction == Direction.LONG
    assert t.bars_held == 0
    assert t.mfe_points == 0.0


def test_from_dict_with_none_direction():
    """from_dict with direction=None should produce a flat tracker."""
    data = {"direction": None, "size": None, "entry_price": None}
    t = PositionTracker.from_dict(data)
    assert t.is_flat()


def test_from_dict_empty_dict():
    """from_dict with empty dict should produce a flat tracker."""
    t = PositionTracker.from_dict({})
    assert t.is_flat()
