"""Position tracking utilities."""

from typing import Any

from tradedesk.marketdata.candle import Candle

from .broker import Direction


class PositionTracker:
    """
    Manages position state for a single instrument within a trading strategy.

    This is a stateful helper class that tracks the core attributes of an
    open position, such as entry price, size, and duration. It also computes
    metrics like Maximum Favorable Excursion (MFE).

    Attributes:
        direction: The current position direction (`Direction.LONG` or
            `Direction.SHORT`), or `None` if flat.
        size: The size of the current position, or `None` if flat.
        entry_price: The execution price of the entry trade, or `None` if flat.
        bars_held: The number of bars the position has been held.
        mfe_points: The Maximum Favorable Excursion in points since the
            position was opened.
    """

    def __init__(self) -> None:
        self.direction: Direction | None = None
        self.size: float | None = None
        self.entry_price: float | None = None
        self.bars_held: int = 0
        self.mfe_points: float = 0.0

    def reset(self) -> None:
        """Reset all position state to flat."""
        self.direction = None
        self.size = None
        self.entry_price = None
        self.bars_held = 0
        self.mfe_points = 0.0

    def is_flat(self) -> bool:
        """Check if no position is held."""
        return self.direction is None

    def open(self, direction: Direction, size: float, entry_price: float) -> None:
        """
        Open a new position.

        Args:
            direction: Position direction (LONG or SHORT)
            size: Position size
            entry_price: Entry price
        """
        self.direction = direction
        self.size = size
        self.entry_price = entry_price
        self.bars_held = 0
        self.mfe_points = 0.0

    def update_mfe(self, candle: Candle) -> None:
        """
        Update maximum favorable excursion using candle extremes.

        Args:
            candle: Current candle with high/low data
        """
        if self.entry_price is None or self.direction is None:
            return

        if self.direction == Direction.LONG:
            favorable = float(candle.high) - self.entry_price
        else:
            favorable = self.entry_price - float(candle.low)

        self.mfe_points = max(self.mfe_points, favorable)

    def current_pnl_points(self, close_price: float) -> float:
        """
        Calculate current PnL in points.

        Args:
            close_price: Current close price

        Returns:
            PnL in points (positive for profit, negative for loss)
        """
        if self.entry_price is None or self.direction is None:
            return 0.0

        if self.direction == Direction.LONG:
            return close_price - self.entry_price
        else:
            return self.entry_price - close_price

    def to_dict(self) -> dict[str, Any]:
        """Serialize current position state to a plain dict (JSON-safe)."""
        return {
            "direction": self.direction.value if self.direction else None,
            "size": self.size,
            "entry_price": self.entry_price,
            "bars_held": self.bars_held,
            "mfe_points": self.mfe_points,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PositionTracker":
        """Restore a PositionTracker from a dict produced by ``to_dict()``."""
        tracker = cls()
        direction_val = data.get("direction")
        if direction_val is not None:
            tracker.direction = Direction(direction_val)
            tracker.size = data.get("size")
            tracker.entry_price = data.get("entry_price")
            tracker.bars_held = data.get("bars_held", 0)
            tracker.mfe_points = data.get("mfe_points", 0.0)
        return tracker
