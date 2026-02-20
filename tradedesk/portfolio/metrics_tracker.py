"""Weighted rolling window performance tracker for risk allocation."""

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .types import Instrument


@dataclass
class InstrumentWindow:
    """Rolling window of trades for a single instrument."""

    trades: deque[dict[str, str | float]] = field(default_factory=deque)
    max_size: int = 1500

    def add_trade(self, trade: dict[str, str | float]) -> None:
        """Add a trade to the window, dropping oldest if at capacity."""
        if len(self.trades) >= self.max_size:
            self.trades.popleft()
        self.trades.append(trade)

    def get_trades(self) -> list[dict[str, str | float]]:
        """Get all trades in the window as a list."""
        return list(self.trades)


@dataclass
class WeightedRollingTracker:
    """
    Maintains a rolling window of trades with decay weighting for performance allocation.

    The window is divided into thirds with decreasing weights:
    - Recent third (most recent N/3 trades): highest weight (default 60%)
    - Middle third (next N/3 trades): medium weight (default 30%)
    - Oldest third (oldest N/3 trades): lowest weight (default 10%)

    This emphasizes recent performance while maintaining historical context.
    """

    window_size: int = 1500  # Total trades to maintain per instrument
    decay_weights: tuple[float, float, float] = (
        0.60,
        0.30,
        0.10,
    )  # recent, middle, old
    recompute_interval: int = 50  # Recompute metrics every N trades

    # Internal state
    _windows: dict[str, InstrumentWindow] = field(default_factory=dict, init=False)
    _trade_count: int = field(default=0, init=False)
    _cached_metrics: dict[str, dict[str, float | int]] | None = field(
        default=None, init=False
    )

    def __post_init__(self) -> None:
        """Validate decay weights sum to 1.0."""
        total = sum(self.decay_weights)
        if abs(total - 1.0) > 0.001:
            raise ValueError(
                f"decay_weights must sum to 1.0, got {total} from {self.decay_weights}"
            )

    def load_from_backtest(self, backtest_dir: Path) -> None:
        """
        Initialize windows from a backtest using the ledger's trade data.

        Loads the most recent window_size trades per instrument from the backtest.
        Uses recording.load_trades_from_backtest() to avoid CSV parsing dependencies.

        Args:
            backtest_dir: Path to backtest results directory containing trades.csv
        """
        from tradedesk.recording import load_trades_from_backtest

        # Load trades using recording domain API
        trades = load_trades_from_backtest(backtest_dir)

        # Group by instrument
        trades_by_instrument: dict[str, list[dict[str, str | float]]] = {}

        for trade in trades:
            instrument = trade["instrument"]
            if instrument not in trades_by_instrument:
                trades_by_instrument[instrument] = []
            trades_by_instrument[instrument].append(trade)

        # Initialize windows with most recent window_size trades per instrument
        for instrument, instrument_trades in trades_by_instrument.items():
            window = InstrumentWindow(max_size=self.window_size)

            # Take last window_size trades (most recent)
            recent_trades = (
                instrument_trades[-self.window_size :]
                if len(instrument_trades) > self.window_size
                else instrument_trades
            )

            for trade in recent_trades:
                window.add_trade(trade)

            self._windows[instrument] = window

        # Reset cache since we loaded new data
        self._cached_metrics = None
        self._trade_count = 0

    def update_from_trades(self, trades: list[dict[str, str | float]]) -> None:
        """
        Add new trades to the rolling windows.

        Args:
            trades: List of trade dicts with keys: instrument, pnl, (other fields optional)
        """
        for trade in trades:
            instrument = str(trade["instrument"])

            if instrument not in self._windows:
                self._windows[instrument] = InstrumentWindow(max_size=self.window_size)

            self._windows[instrument].add_trade(trade)
            self._trade_count += 1

        # Invalidate cache if we've crossed recompute threshold
        if self._trade_count >= self.recompute_interval:
            self._cached_metrics = None
            self._trade_count = 0

    def compute_metrics(
        self, instruments: list[Instrument]
    ) -> Mapping[Instrument, dict[str, float | int]]:
        """
        Compute weighted performance metrics for given instruments.

        Applies decay weighting to the rolling window:
        - Recent third gets highest weight
        - Middle third gets medium weight
        - Oldest third gets lowest weight

        Returns metrics dict with:
        - 'return_to_risk_ratio': sum(weighted pnls) / sum(abs(weighted pnls))
        - 'total_trades': number of trades in window
        - 'weighted_pnl': total weighted PnL

        Args:
            instruments: List of instruments to compute metrics for

        Returns:
            Mapping from Instrument to metrics dict
        """
        # Check if we need to recompute (cache miss for any requested instrument)
        need_recompute = self._cached_metrics is None or any(
            str(inst) not in self._cached_metrics for inst in instruments
        )

        if not need_recompute and self._cached_metrics is not None:
            # All requested instruments are in cache
            return {inst: self._cached_metrics[str(inst)] for inst in instruments}

        # Compute fresh metrics for ALL instruments in windows (not just requested)
        # This ensures cache is complete for future calls
        all_metrics: dict[str, dict[str, float | int]] = {}

        for instrument, window in self._windows.items():
            trades = window.get_trades()

            if len(trades) == 0:
                all_metrics[instrument] = self._empty_metrics()
                continue

            # Apply decay weighting to trades
            weighted_pnls = self._apply_decay_weights(trades)

            # Compute weighted return/risk ratio
            total_weighted_pnl = sum(weighted_pnls)
            total_weighted_risk = sum(abs(p) for p in weighted_pnls)

            return_to_risk = (
                total_weighted_pnl / total_weighted_risk
                if total_weighted_risk > 0
                else 0.0
            )

            all_metrics[instrument] = {
                "return_to_risk_ratio": return_to_risk,
                "total_trades": len(trades),
                "weighted_pnl": total_weighted_pnl,
            }

        # Cache all computed metrics
        self._cached_metrics = all_metrics

        # Return metrics for requested instruments
        metrics_by_instrument: dict[Instrument, dict[str, float | int]] = {}

        for inst in instruments:
            instrument = str(inst)
            metrics_by_instrument[inst] = all_metrics.get(
                instrument, self._empty_metrics()
            )

        return metrics_by_instrument

    def _apply_decay_weights(self, trades: list[dict[str, str | float]]) -> list[float]:
        """
        Apply decay weights to trades based on position in window.

        Divides trades into thirds and applies decay_weights:
        - Most recent third: decay_weights[0]
        - Middle third: decay_weights[1]
        - Oldest third: decay_weights[2]

        Args:
            trades: List of trades (ordered oldest to newest)

        Returns:
            List of weighted PnL values
        """
        n = len(trades)

        if n == 0:
            return []

        # Calculate third sizes
        third_size = n // 3
        remainder = n % 3

        # Distribute remainder to make thirds as equal as possible
        # Put extra trades in older thirds to keep recent third tight
        old_size = third_size + (1 if remainder > 0 else 0)
        mid_size = third_size + (1 if remainder > 1 else 0)

        # Split trades into thirds (oldest -> newest)
        old_end = old_size
        mid_start = old_end
        mid_end = mid_start + mid_size

        weighted_pnls: list[float] = []

        # Apply weights to each third
        for i, trade in enumerate(trades):
            pnl = float(trade["pnl"])

            if i < old_end:
                # Oldest third
                weight = self.decay_weights[2]
            elif i < mid_end:
                # Middle third
                weight = self.decay_weights[1]
            else:
                # Recent third
                weight = self.decay_weights[0]

            weighted_pnls.append(pnl * weight)

        return weighted_pnls

    def _empty_metrics(self) -> dict[str, float | int]:
        """Return empty metrics for instruments with no data."""
        return {
            "return_to_risk_ratio": 0.0,
            "total_trades": 0,
            "weighted_pnl": 0.0,
        }
