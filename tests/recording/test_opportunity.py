"""Tests for tradedesk.recording.opportunity – opportunity tracking."""

import pytest

from tradedesk.recording.opportunity import InstrumentOpportunity, OpportunityRecorder

# ---------------------------------------------------------------------------
# InstrumentOpportunity
# ---------------------------------------------------------------------------


class TestInstrumentOpportunity:
    def test_initial_state(self):
        opp = InstrumentOpportunity()
        assert opp.bars == 0
        assert opp.regime_active_bars == 0
        assert opp.regime_on_count == 0

    def test_active_bar_increments(self):
        opp = InstrumentOpportunity()
        opp.on_bar(active=True)
        assert opp.bars == 1
        assert opp.regime_active_bars == 1
        assert opp.regime_on_count == 1

    def test_inactive_bar(self):
        opp = InstrumentOpportunity()
        opp.on_bar(active=False)
        assert opp.bars == 1
        assert opp.regime_active_bars == 0
        assert opp.regime_on_count == 0

    def test_transition_counting(self):
        opp = InstrumentOpportunity()
        # False -> True transition
        opp.on_bar(active=False)
        opp.on_bar(active=True)
        assert opp.regime_on_count == 1

        # True -> True (no new transition)
        opp.on_bar(active=True)
        assert opp.regime_on_count == 1

        # True -> False -> True (new transition)
        opp.on_bar(active=False)
        opp.on_bar(active=True)
        assert opp.regime_on_count == 2

    def test_first_bar_active_counts_as_transition(self):
        """First bar being active should count as a transition (from None)."""
        opp = InstrumentOpportunity()
        opp.on_bar(active=True)
        assert opp.regime_on_count == 1

    def test_regime_active_pct_zero_bars(self):
        opp = InstrumentOpportunity()
        assert opp.regime_active_pct() == 0.0

    def test_regime_active_pct(self):
        opp = InstrumentOpportunity()
        opp.on_bar(active=True)
        opp.on_bar(active=True)
        opp.on_bar(active=False)
        opp.on_bar(active=False)
        assert opp.regime_active_pct() == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# OpportunityRecorder
# ---------------------------------------------------------------------------


class TestOpportunityRecorder:
    def test_on_instrument_bar_creates_entry(self):
        rec = OpportunityRecorder()
        rec.on_instrument_bar(
            instrument="USDJPY", timestamp="2025-01-01T00:00:00Z", active=True
        )
        assert "USDJPY" in rec.per_instrument
        assert rec.per_instrument["USDJPY"].bars == 1

    def test_on_instrument_bar_multiple_instruments(self):
        rec = OpportunityRecorder()
        rec.on_instrument_bar(instrument="USDJPY", timestamp="t1", active=True)
        rec.on_instrument_bar(instrument="GBPUSD", timestamp="t1", active=False)
        assert len(rec.per_instrument) == 2
        assert rec.per_instrument["USDJPY"].regime_active_bars == 1
        assert rec.per_instrument["GBPUSD"].regime_active_bars == 0

    def test_on_portfolio_snapshot(self):
        rec = OpportunityRecorder()
        rec.on_portfolio_snapshot(timestamp="t1", k_active=2)
        rec.on_portfolio_snapshot(timestamp="t2", k_active=3)
        assert rec.k_active_series() == [2, 3]

    def test_on_portfolio_snapshot_coalesces_same_timestamp(self):
        rec = OpportunityRecorder()
        rec.on_portfolio_snapshot(timestamp="t1", k_active=1)
        rec.on_portfolio_snapshot(timestamp="t1", k_active=2)
        # Should coalesce: only one entry, updated to 2
        assert rec.k_active_series() == [2]

    def test_avg_k_active(self):
        rec = OpportunityRecorder()
        rec.on_portfolio_snapshot(timestamp="t1", k_active=2)
        rec.on_portfolio_snapshot(timestamp="t2", k_active=4)
        assert rec.avg_k_active() == pytest.approx(3.0)

    def test_avg_k_active_empty(self):
        rec = OpportunityRecorder()
        assert rec.avg_k_active() == 0.0

    def test_p95_k_active(self):
        rec = OpportunityRecorder()
        for i in range(100):
            rec.on_portfolio_snapshot(timestamp=f"t{i}", k_active=i)
        assert rec.p95_k_active() == pytest.approx(94.0, abs=1.0)

    def test_p95_k_active_empty(self):
        rec = OpportunityRecorder()
        assert rec.p95_k_active() == 0.0

    def test_max_k_active(self):
        rec = OpportunityRecorder()
        rec.on_portfolio_snapshot(timestamp="t1", k_active=1)
        rec.on_portfolio_snapshot(timestamp="t2", k_active=5)
        rec.on_portfolio_snapshot(timestamp="t3", k_active=3)
        assert rec.max_k_active() == 5

    def test_max_k_active_empty(self):
        rec = OpportunityRecorder()
        assert rec.max_k_active() == 0
