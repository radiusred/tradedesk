"""Tests for tradedesk.portfolio.metrics_tracker – weighted rolling window tracker."""

import csv

import pytest

from tradedesk.portfolio.metrics_tracker import InstrumentWindow, WeightedRollingTracker
from tradedesk.portfolio.types import Instrument


# ---------------------------------------------------------------------------
# InstrumentWindow
# ---------------------------------------------------------------------------


class TestInstrumentWindow:
    def test_add_and_get(self):
        w = InstrumentWindow(max_size=5)
        w.add_trade({"pnl": 10.0})
        w.add_trade({"pnl": -5.0})
        assert len(w.get_trades()) == 2

    def test_max_size_eviction(self):
        w = InstrumentWindow(max_size=3)
        for i in range(5):
            w.add_trade({"pnl": float(i)})
        trades = w.get_trades()
        assert len(trades) == 3
        # Oldest should be evicted
        assert trades[0]["pnl"] == 2.0

    def test_get_trades_returns_list_copy(self):
        w = InstrumentWindow()
        w.add_trade({"pnl": 1.0})
        trades = w.get_trades()
        assert isinstance(trades, list)


# ---------------------------------------------------------------------------
# WeightedRollingTracker
# ---------------------------------------------------------------------------


class TestWeightedRollingTracker:
    def test_invalid_decay_weights(self):
        with pytest.raises(ValueError, match="must sum to 1.0"):
            WeightedRollingTracker(decay_weights=(0.5, 0.3, 0.1))

    def test_update_from_trades(self):
        tracker = WeightedRollingTracker()
        tracker.update_from_trades(
            [
                {"instrument": "USDJPY", "pnl": 10.0},
                {"instrument": "USDJPY", "pnl": -5.0},
                {"instrument": "GBPUSD", "pnl": 3.0},
            ]
        )
        assert "USDJPY" in tracker._windows
        assert "GBPUSD" in tracker._windows
        assert len(tracker._windows["USDJPY"].get_trades()) == 2

    def test_cache_invalidation(self):
        tracker = WeightedRollingTracker(recompute_interval=2)
        tracker.update_from_trades([{"instrument": "X", "pnl": 1.0}])
        # Force cache
        tracker.compute_metrics([Instrument("X")])
        assert tracker._cached_metrics is not None

        # Two more trades should trigger recompute
        tracker.update_from_trades(
            [
                {"instrument": "X", "pnl": 2.0},
                {"instrument": "X", "pnl": 3.0},
            ]
        )
        assert tracker._cached_metrics is None

    def test_compute_metrics_basic(self):
        tracker = WeightedRollingTracker(decay_weights=(0.6, 0.3, 0.1))
        trades = [{"instrument": "USDJPY", "pnl": 10.0} for _ in range(9)]
        tracker.update_from_trades(trades)

        metrics = tracker.compute_metrics([Instrument("USDJPY")])
        m = metrics[Instrument("USDJPY")]
        assert m["total_trades"] == 9
        assert m["weighted_pnl"] > 0
        assert m["return_to_risk_ratio"] == pytest.approx(1.0)  # all positive

    def test_compute_metrics_all_negative(self):
        tracker = WeightedRollingTracker(decay_weights=(0.6, 0.3, 0.1))
        trades = [{"instrument": "X", "pnl": -5.0} for _ in range(6)]
        tracker.update_from_trades(trades)

        metrics = tracker.compute_metrics([Instrument("X")])
        assert metrics[Instrument("X")]["return_to_risk_ratio"] == pytest.approx(-1.0)

    def test_compute_metrics_empty_instrument(self):
        tracker = WeightedRollingTracker()
        metrics = tracker.compute_metrics([Instrument("UNKNOWN")])
        m = metrics[Instrument("UNKNOWN")]
        assert m["total_trades"] == 0
        assert m["return_to_risk_ratio"] == 0.0

    def test_compute_metrics_caching(self):
        tracker = WeightedRollingTracker(recompute_interval=100)
        tracker.update_from_trades([{"instrument": "X", "pnl": 5.0}])
        m1 = tracker.compute_metrics([Instrument("X")])
        # Should use cache on second call
        m2 = tracker.compute_metrics([Instrument("X")])
        assert m1[Instrument("X")] == m2[Instrument("X")]

    def test_apply_decay_weights_thirds(self):
        tracker = WeightedRollingTracker(decay_weights=(0.6, 0.3, 0.1))
        # 9 trades: 3 old (weight 0.1), 3 mid (weight 0.3), 3 recent (weight 0.6)
        trades = [{"pnl": 10.0} for _ in range(9)]
        weighted = tracker._apply_decay_weights(trades)
        assert len(weighted) == 9
        # Old third
        assert weighted[0] == pytest.approx(10.0 * 0.1)
        assert weighted[2] == pytest.approx(10.0 * 0.1)
        # Middle third
        assert weighted[3] == pytest.approx(10.0 * 0.3)
        # Recent third
        assert weighted[6] == pytest.approx(10.0 * 0.6)

    def test_apply_decay_weights_empty(self):
        tracker = WeightedRollingTracker()
        assert tracker._apply_decay_weights([]) == []

    def test_apply_decay_weights_remainder(self):
        """Non-divisible-by-3 trade counts distribute remainder to older thirds."""
        tracker = WeightedRollingTracker(decay_weights=(0.6, 0.3, 0.1))
        # 10 trades: old=4, mid=3, recent=3
        trades = [{"pnl": 10.0} for _ in range(10)]
        weighted = tracker._apply_decay_weights(trades)
        assert len(weighted) == 10
        # First 4 should have old weight
        assert weighted[3] == pytest.approx(10.0 * 0.1)
        # Next 3 should have mid weight
        assert weighted[4] == pytest.approx(10.0 * 0.3)

    def test_load_from_backtest(self, tmp_path):
        # Create a trades.csv
        csv_path = tmp_path / "trades.csv"
        with csv_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "instrument", "direction", "size", "price"])
            # Round trip 1
            w.writerow(["2025-01-01T00:00:00Z", "USDJPY", "BUY", "1.0", "150.0"])
            w.writerow(["2025-01-01T01:00:00Z", "USDJPY", "SELL", "1.0", "155.0"])
            # Round trip 2
            w.writerow(["2025-01-02T00:00:00Z", "GBPUSD", "SELL", "2.0", "1.3000"])
            w.writerow(["2025-01-02T01:00:00Z", "GBPUSD", "BUY", "2.0", "1.2900"])

        tracker = WeightedRollingTracker(window_size=100)
        tracker.load_from_backtest(tmp_path)

        assert "USDJPY" in tracker._windows
        assert "GBPUSD" in tracker._windows
        assert len(tracker._windows["USDJPY"].get_trades()) == 1
        assert len(tracker._windows["GBPUSD"].get_trades()) == 1

    def test_load_from_backtest_missing_file(self, tmp_path):
        tracker = WeightedRollingTracker()
        with pytest.raises(FileNotFoundError):
            tracker.load_from_backtest(tmp_path)

    def test_load_from_backtest_empty_file(self, tmp_path):
        csv_path = tmp_path / "trades.csv"
        with csv_path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "instrument", "direction", "size", "price"])

        tracker = WeightedRollingTracker()
        with pytest.raises(ValueError, match="No trades found"):
            tracker.load_from_backtest(tmp_path)

    def test_compute_metrics_forces_recompute_for_new_instrument(self):
        tracker = WeightedRollingTracker(recompute_interval=100)
        tracker.update_from_trades([{"instrument": "X", "pnl": 5.0}])
        # Cache for X
        tracker.compute_metrics([Instrument("X")])
        assert tracker._cached_metrics is not None

        # Add Y and ask for it – should recompute since Y not in cache
        tracker.update_from_trades([{"instrument": "Y", "pnl": 3.0}])
        metrics = tracker.compute_metrics([Instrument("Y")])
        assert Instrument("Y") in metrics
