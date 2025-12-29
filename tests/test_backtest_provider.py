from unittest.mock import patch

from tradedesk import run_strategies
from tradedesk.marketdata import Candle, CandleClose, MarketData
from tradedesk.providers.backtest.client import BacktestClient
from tradedesk.strategy import BaseStrategy
from tradedesk.subscriptions import ChartSubscription


def test_backtest_replays_candles_and_executes_virtual_trades():
    # Simple 3-candle series
    candles = [
        Candle(timestamp="2025-12-28T00:00:00Z", open=10, high=10, low=10, close=10, volume=0.0, tick_count=0),
        Candle(timestamp="2025-12-28T00:05:00Z", open=11, high=11, low=11, close=11, volume=0.0, tick_count=0),
        Candle(timestamp="2025-12-28T00:10:00Z", open=12, high=12, low=12, close=12, volume=0.0, tick_count=0),
    ]

    history = {("EPIC", "5MINUTE"): candles}

    created: dict[str, object] = {}
    seen = []

    class TradeOnFirstLast(BaseStrategy):
        SUBSCRIPTIONS = [ChartSubscription("EPIC", "5MINUTE")]

        async def on_price_update(self, md: MarketData):
            pass

        async def on_candle_close(self, cc: CandleClose):
            # record receipt
            seen.append(cc.candle.close)

            # trade: buy on first candle, sell on last candle
            if cc.candle.close == 10:
                await self.client.place_market_order(epic=cc.epic, direction="BUY", size=1.0)
            if cc.candle.close == 12:
                await self.client.place_market_order(epic=cc.epic, direction="SELL", size=1.0)

            # keep default chart storage behavior
            await super().on_candle_close(cc)

    def factory():
        c = BacktestClient.from_history(history)
        created["client"] = c
        return c

    # Avoid sys.exit() if something goes wrong; we want a clean test failure instead.
    with patch("sys.exit") as _:
        run_strategies(
            strategy_specs=[TradeOnFirstLast],
            client_factory=factory,
            setup_logging=False,
        )

    client: BacktestClient = created["client"]  # type: ignore[assignment]

    # Candles were replayed
    assert seen == [10, 11, 12]

    # Trades recorded
    assert len(client.trades) == 2
    assert client.trades[0].direction == "BUY"
    assert client.trades[0].price == 10.0
    assert client.trades[1].direction == "SELL"
    assert client.trades[1].price == 12.0

    # Position netted out, realised PnL computed: (12 - 10) * 1 = 2
    assert client.positions == {}
    assert client.realised_pnl == 2.0
