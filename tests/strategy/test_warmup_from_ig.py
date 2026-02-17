import pytest

from tradedesk.marketdata.indicators.williams_r import WilliamsR
from tradedesk.marketdata.subscriptions import ChartSubscription


@pytest.mark.asyncio
class TestStrategyWarmupFromIG:
    async def test_warmup_from_ig_primes_chart_and_indicator(
        self, DummyStrategy, make_candles
    ):
        sub = ChartSubscription("EPIC", "1MINUTE")
        Strat = DummyStrategy([sub])

        class FakeClient:
            async def get_historical_candles(self, instrument, period, num_points):
                assert instrument == "EPIC"
                assert period == "1MINUTE"
                assert num_points == 14
                return make_candles(14)

        strat = Strat(data_provider=FakeClient())

        wr = WilliamsR(period=14)
        strat.register_indicator(sub, wr)

        await strat.warmup()

        assert len(strat.charts[("EPIC", "1MINUTE")]) == 14
        assert wr.ready() is True
