from tradedesk.subscriptions import ChartSubscription
from tradedesk.indicators.macd import MACD
from tradedesk.indicators.mfi import MFI
from tradedesk.indicators.williams_r import WilliamsR
import pytest

@pytest.fixture
def strat(DummyStrategy):
    Strat = DummyStrategy([])
    return Strat(client=None)

class TestStrategyIndicatorWarmup:
    def test_no_indicators_returns_zero(self, strat):
        sub = ChartSubscription("EPIC", "1MINUTE")
        assert strat.required_warmup(sub) == 0

    def test_single_indicator_warmup(self, strat):
        sub = ChartSubscription("EPIC", "1MINUTE")
        wr = WilliamsR(period=14)

        strat.register_indicator(sub, wr)

        assert strat.required_warmup(sub) == 14

    def test_multiple_indicators_uses_max_warmup(self, strat):
        sub = ChartSubscription("EPIC", "1MINUTE")

        wr = WilliamsR(period=14)
        mfi = MFI(period=14)  # warmup = 15
        macd = MACD(fast=12, slow=26, signal=9)  # warmup = 34

        strat.register_indicator(sub, wr)
        strat.register_indicator(sub, mfi)
        strat.register_indicator(sub, macd)

        assert strat.required_warmup(sub) == 34

    def test_indicators_are_isolated_per_chart(self, strat):
        sub1 = ChartSubscription("EPIC1", "1MINUTE")
        sub2 = ChartSubscription("EPIC2", "5MINUTE")

        wr = WilliamsR(period=14)
        macd = MACD(fast=12, slow=26, signal=9)

        strat.register_indicator(sub1, wr)
        strat.register_indicator(sub2, macd)

        assert strat.required_warmup(sub1) == 14
        assert strat.required_warmup(sub2) == 34
        assert strat.required_warmup(ChartSubscription("EPIC1", "5MINUTE")) == 0
