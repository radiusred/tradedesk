from unittest.mock import patch

import pytest

from tradedesk import run_strategies
from tradedesk.marketdata.instrument import MarketData
from tradedesk.types import Candle
from tradedesk.marketdata.events import CandleClosedEvent
from tradedesk.execution.backtest.client import BacktestClient
from tradedesk.strategy.base import BaseStrategy
from tradedesk.marketdata.subscriptions import ChartSubscription
from tradedesk.execution.broker import Direction


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

        async def on_candle_close(self, cc: CandleClosedEvent):
            # record receipt
            seen.append(cc.candle.close)

            # trade: buy on first candle, sell on last candle
            if cc.candle.close == 10:
                await self.client.place_market_order(instrument=cc.instrument, direction="BUY", size=1.0)
            if cc.candle.close == 12:
                await self.client.place_market_order(instrument=cc.instrument, direction="SELL", size=1.0)

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
    assert client.trades[0].direction == Direction.LONG
    assert client.trades[0].price == 10.0
    assert client.trades[1].direction == Direction.SHORT
    assert client.trades[1].price == 12.0

    # Position netted out, realised PnL computed: (12 - 10) * 1 = 2
    assert client.positions == {}
    assert client.realised_pnl == 2.0


async def test_backtest_rejects_direction_enum_without_conversion():
    """Test that passing Direction enum directly to place_market_order is rejected."""
    candles = [
        Candle(timestamp="2025-12-28T00:00:00Z", open=100, high=100, low=100, close=100),
    ]
    client = BacktestClient.from_history({("TEST", "1MINUTE"): candles})
    await client.start()
    client._set_mark_price("TEST", 100.0)

    # Passing Direction enum directly should raise ValueError
    with pytest.raises(ValueError, match="Invalid order side"):
        await client.place_market_order(
            instrument="TEST",
            direction=Direction.LONG,  # This is wrong!
            size=1.0
        )


async def test_backtest_accepts_direction_with_to_order_side():
    """Test that using Direction.to_order_side() works correctly."""
    candles = [
        Candle(timestamp="2025-12-28T00:00:00Z", open=100, high=100, low=100, close=100),
        Candle(timestamp="2025-12-28T00:05:00Z", open=110, high=110, low=110, close=110),
    ]
    client = BacktestClient.from_history({("TEST", "5MINUTE"): candles})
    await client.start()
    client._set_mark_price("TEST", 100.0)
    client._set_current_timestamp("2025-12-28T00:00:00Z")

    # Using to_order_side() should work
    result = await client.place_market_order(
        instrument="TEST",
        direction=Direction.LONG.to_order_side(),  # Correct!
        size=1.0
    )

    assert result["status"] == "FILLED"
    assert result["direction"] == Direction.LONG
    assert len(client.positions) == 1
    assert client.positions["TEST"].direction == Direction.LONG

    # Close with opposite direction
    client._set_mark_price("TEST", 110.0)
    client._set_current_timestamp("2025-12-28T00:05:00Z")

    result = await client.place_market_order(
        instrument="TEST",
        direction=Direction.SHORT.to_order_side(),  # SELL
        size=1.0
    )

    assert result["status"] == "FILLED"
    assert result["direction"] == Direction.SHORT
    assert len(client.positions) == 0  # Position closed
    assert client.realised_pnl == 10.0  # (110 - 100) * 1.0
