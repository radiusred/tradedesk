import pytest

from tradedesk.marketdata.events import CandleClosedEvent
from tradedesk.marketdata.subscriptions import ChartSubscription


@pytest.mark.asyncio
class TestOnCandleUpdateDefault:
    async def test_on_candle_update_stores_in_chart_history(
        self, DummyStrategy, candle_factory
    ):
        sub = ChartSubscription("EPIC", "1MINUTE")
        Strat = DummyStrategy([sub])
        strat = Strat(data_provider=None)

        candle = candle_factory(0)
        await strat.on_candle_close(
            CandleClosedEvent(instrument="EPIC", timeframe="1MINUTE", candle=candle)
        )

        chart = strat.charts[("EPIC", "1MINUTE")]
        assert len(chart) == 1
        assert chart.latest == candle

    async def test_on_candle_update_no_chart_key_is_noop(
        self, DummyStrategy, candle_factory
    ):
        Strat = DummyStrategy([])  # no chart subs => no chart history entries created
        strat = Strat(data_provider=None)

        # Should not raise
        await strat.on_candle_close(
            CandleClosedEvent(
                instrument="EPIC", timeframe="1MINUTE", candle=candle_factory(0)
            )
        )
