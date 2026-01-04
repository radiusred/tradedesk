from tradedesk.subscriptions import ChartSubscription, MarketSubscription
from tradedesk.indicators.macd import MACD
from tradedesk.indicators.williams_r import WilliamsR


class TestStrategyWarmupPlan:
    def test_plan_includes_only_chart_subscriptions(self, DummyStrategy):
        Strat = DummyStrategy([
            MarketSubscription("MARKET_EPIC"),
            ChartSubscription("EPIC1", "1MINUTE"),
            ChartSubscription("EPIC2", "5MINUTE"),
        ])
        strat = Strat(client=None)

        plan = strat.chart_warmup_plan()

        assert ("EPIC1", "1MINUTE") in plan
        assert ("EPIC2", "5MINUTE") in plan
        # Market subs should not appear
        assert ("MARKET_EPIC", "MARKET") not in plan  # sanity check

    def test_plan_defaults_to_zero_when_no_indicators_registered(self, DummyStrategy):
        Strat = DummyStrategy([
            ChartSubscription("EPIC1", "1MINUTE"),
            ChartSubscription("EPIC2", "5MINUTE"),
        ])
        strat = Strat(client=None)

        plan = strat.chart_warmup_plan()

        assert plan[("EPIC1", "1MINUTE")] == 0
        assert plan[("EPIC2", "5MINUTE")] == 0

    def test_plan_uses_required_warmup_per_chart(self, DummyStrategy):
        Strat = DummyStrategy([
            ChartSubscription("EPIC1", "1MINUTE"),
            ChartSubscription("EPIC2", "5MINUTE"),
        ])
        strat = Strat(client=None)

        sub1 = ChartSubscription("EPIC1", "1MINUTE")

        strat.register_indicator(sub1, WilliamsR(period=14))  # warmup 14
        strat.register_indicator(sub1, MACD(fast=12, slow=26, signal=9))  # warmup 34
        # EPIC2 intentionally has no indicators

        plan = strat.chart_warmup_plan()

        assert plan[("EPIC1", "1MINUTE")] == 34
        assert plan[("EPIC2", "5MINUTE")] == 0
