from tradedesk.subscriptions import ChartSubscription
from tradedesk.indicators.williams_r import WilliamsR


class TestStrategyChartPriming:
    def test_prime_chart_populates_history_and_indicators(self, DummyStrategy, make_candles):
        Strat = DummyStrategy([ChartSubscription("EPIC", "1MINUTE")])
        strat = Strat(client=None)

        sub = ChartSubscription("EPIC", "1MINUTE")
        wr = WilliamsR(period=3)
        strat.register_indicator(sub, wr)

        strat.prime_chart(sub, make_candles(3))

        chart = strat.charts[("EPIC", "1MINUTE")]
        assert len(chart) == 3
        assert wr.ready() is True