from tradedesk.subscriptions import ChartSubscription
from tradedesk.indicators.williams_r import WilliamsR


class TestWarmupFromHistory:

    def test_warmup_primes_only_charts_with_history(self, DummyStrategy, make_candles):
        Strat = DummyStrategy([
            ChartSubscription("EPIC1", "1MINUTE"),
            ChartSubscription("EPIC2", "5MINUTE"),
        ])
        strat = Strat(client=None)

        sub1 = ChartSubscription("EPIC1", "1MINUTE")
        wr = WilliamsR(period=3)
        strat.register_indicator(sub1, wr)

        history = {
            ("EPIC1", "1MINUTE"): make_candles(3),
            # EPIC2 intentionally omitted
        }

        strat.warmup_from_history(history)

        assert len(strat.charts[("EPIC1", "1MINUTE")]) == 3
        assert wr.ready() is True
        assert len(strat.charts[("EPIC2", "5MINUTE")]) == 0
