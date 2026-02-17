"""Tests for tradedesk.types — shared domain types."""

from tradedesk.types import Direction


class TestDirection:
    """Tests for the Direction enum."""

    def test_opposite(self) -> None:
        assert Direction.LONG.opposite() == Direction.SHORT
        assert Direction.SHORT.opposite() == Direction.LONG

    def test_to_order_side(self) -> None:
        assert Direction.LONG.to_order_side() == "BUY"
        assert Direction.SHORT.to_order_side() == "SELL"

    def test_to_order_side_returns_string(self) -> None:
        result = Direction.LONG.to_order_side()
        assert isinstance(result, str)
        assert result == "BUY"

    def test_from_order_side(self) -> None:
        assert Direction.from_order_side("BUY") == Direction.LONG
        assert Direction.from_order_side("SELL") == Direction.SHORT

    def test_from_order_side_case_insensitive(self) -> None:
        assert Direction.from_order_side("buy") == Direction.LONG
        assert Direction.from_order_side("Sell") == Direction.SHORT
