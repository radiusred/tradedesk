"""Tests for portfolio runner."""

import pytest

from tradedesk.portfolio.risk import EqualSplitRiskPolicy
from tradedesk.portfolio.runner import PortfolioRunner
from tradedesk.portfolio.types import Instrument
from tradedesk.marketdata.events import CandleClosedEvent


class FakeStrategy:
    """Minimal fake strategy for testing PortfolioRunner."""

    def __init__(self, instrument: str, *, active: bool):
        self.instrument = Instrument(instrument)
        self._active = active
        self._rpt = None
        self.update_state_calls = 0
        self.evaluate_signals_calls = 0

    def is_regime_active(self) -> bool:
        return self._active

    def set_risk_per_trade(self, value: float) -> None:
        self._rpt = float(value)

    async def update_state(self, event: CandleClosedEvent) -> None:
        self.update_state_calls += 1

    async def evaluate_signals(self) -> None:
        self.evaluate_signals_calls += 1


@pytest.mark.asyncio
async def test_runner_splits_risk_across_active_strategies():
    """Test that PortfolioRunner splits risk budget across active strategies."""
    s1 = FakeStrategy("EURUSD", active=True)
    s2 = FakeStrategy("GBPUSD", active=True)
    s3 = FakeStrategy("USDJPY", active=False)

    r = PortfolioRunner(
        strategies={
            Instrument("EURUSD"): s1,
            Instrument("GBPUSD"): s2,
            Instrument("USDJPY"): s3,
        },
        policy=EqualSplitRiskPolicy(portfolio_risk_budget=10.0),
        default_risk_per_trade=10.0,
    )

    await r.on_candle_close(
        CandleClosedEvent(
            instrument=Instrument("EURUSD"), timeframe="15MINUTE", candle=None
        )
    )

    # Two active strategies should get 5.0 each
    assert s1._rpt == 5.0
    assert s2._rpt == 5.0
    # Inactive strategy should get default
    assert s3._rpt == 10.0
    # Only the strategy for the candle's instrument should process the event
    assert s1.update_state_calls == 1
    assert s1.evaluate_signals_calls == 1
    assert s2.update_state_calls == 0
    assert s2.evaluate_signals_calls == 0
