import pytest

from tradedesk.marketdata import CandleClose
from tradedesk.subscriptions import ChartSubscription


@pytest.mark.asyncio
class TestOnCandleUpdateDefault:
    async def test_on_candle_update_stores_in_chart_history(self, DummyStrategy, candle_factory):
        sub = ChartSubscription("EPIC", "1MINUTE")
        Strat = DummyStrategy([sub])
        strat = Strat(client=None)

        candle = candle_factory(0)
        await strat.on_candle_close(CandleClose("EPIC", "1MINUTE", candle))

        chart = strat.charts[("EPIC", "1MINUTE")]
        assert len(chart) == 1
        assert chart.latest == candle

    async def test_on_candle_update_no_chart_key_is_noop(self, DummyStrategy, candle_factory):
        Strat = DummyStrategy([])  # no chart subs => no chart history entries created
        strat = Strat(client=None)

        # Should not raise
        await strat.on_candle_close(CandleClose("EPIC", "1MINUTE", candle_factory(0)))
