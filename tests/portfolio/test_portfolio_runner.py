"""Tests for portfolio runner."""

import pytest

from tradedesk.marketdata.events import CandleClosedEvent
from tradedesk.portfolio.risk import EqualSplitRiskPolicy
from tradedesk.portfolio.runner import PortfolioRunner
from tradedesk.portfolio.types import Instrument, SleeveId


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
    """Test that PortfolioRunner splits risk budget across active sleeves."""
    s1 = FakeStrategy("EURUSD", active=True)
    s2 = FakeStrategy("GBPUSD", active=True)
    s3 = FakeStrategy("USDJPY", active=False)

    r = PortfolioRunner(
        strategies={
            SleeveId("s1"): s1,
            SleeveId("s2"): s2,
            SleeveId("s3"): s3,
        },
        policy=EqualSplitRiskPolicy(portfolio_risk_budget=10.0),
        default_risk_per_trade=10.0,
    )

    await r.on_candle_close(
        CandleClosedEvent(instrument=Instrument("EURUSD"), timeframe="15MINUTE", candle=None)
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


@pytest.mark.asyncio
async def test_runner_fans_out_to_dual_sleeve_same_instrument():
    """Both sleeves on the same instrument receive the candle and share risk budget."""
    fade = FakeStrategy("AUDCAD", active=True)
    bollinger = FakeStrategy("AUDCAD", active=True)
    other = FakeStrategy("EURUSD", active=False)

    r = PortfolioRunner(
        strategies={
            SleeveId("AdaptiveFade_AUDCAD"): fade,
            SleeveId("BollingerReversion_AUDCAD"): bollinger,
            SleeveId("BollingerReversion_EURUSD"): other,
        },
        policy=EqualSplitRiskPolicy(portfolio_risk_budget=10.0),
        default_risk_per_trade=10.0,
    )

    await r.on_candle_close(
        CandleClosedEvent(instrument=Instrument("AUDCAD"), timeframe="15MINUTE", candle=None)
    )

    # Both AUDCAD sleeves active -> 5.0 each (2 active out of 3 sleeves)
    assert fade._rpt == 5.0
    assert bollinger._rpt == 5.0
    # Inactive EURUSD sleeve gets default
    assert other._rpt == 10.0
    # Both AUDCAD sleeves should have processed the candle
    assert fade.update_state_calls == 1
    assert fade.evaluate_signals_calls == 1
    assert bollinger.update_state_calls == 1
    assert bollinger.evaluate_signals_calls == 1
    # EURUSD sleeve should not have processed the AUDCAD candle
    assert other.update_state_calls == 0
    assert other.evaluate_signals_calls == 0
