import pytest
from unittest.mock import AsyncMock, MagicMock

from tradedesk.marketdata import Candle, MarketData
from tradedesk.marketdata import CandleClose
from tradedesk.strategy import BaseStrategy

from unittest.mock import AsyncMock

class Strat(BaseStrategy):
    SUBSCRIPTIONS = []

    def __init__(self, client):
        super().__init__(client)
        self.on_price_update_mock = AsyncMock()
        self.on_candle_update_mock = AsyncMock()

    async def on_price_update(self, md: MarketData):
        await self.on_price_update_mock(md)

    async def on_candle_close(self, cc: CandleClose):
        await self.on_candle_update_mock(cc)
        await super().on_candle_close(cc)

@pytest.mark.asyncio
async def test_handle_event_marketdata_updates_last_update_and_dispatches():
    s = Strat(MagicMock())
    before = s.last_update

    event = MarketData(
        epic="EPIC",
        bid=1.0,
        offer=1.1,
        timestamp="2025-12-28T00:00:00Z",
        raw={"foo": "bar"},
    )

    await s._handle_event(event)

    assert s.last_update >= before
    s.on_price_update_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_handle_event_candleclose_dispatches_and_uses_default_storage():
    # ensure chart storage path is covered: add a chart by defining a chart subscription
    from tradedesk.subscriptions import ChartSubscription

    class S(BaseStrategy):
        SUBSCRIPTIONS = [ChartSubscription("EPIC", "5MINUTE")]

        async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
            pass

    s = S(MagicMock())

    candle = Candle(
        timestamp="2025-12-28T00:00:00Z",
        open=1.0,
        high=1.2,
        low=0.9,
        close=1.1,
        volume=10.0,
        tick_count=3,
    )
    event = CandleClose(epic="EPIC", period="5MINUTE", candle=candle)

    await s._handle_event(event)

    key = ("EPIC", "5MINUTE")
    assert key in s.charts
    assert s.charts[key].get_candles()[-1] == candle


@pytest.mark.asyncio
async def test_handle_event_rejects_unknown_type():
    s = Strat(MagicMock())
    with pytest.raises(TypeError):
        await s._handle_event(object())  # type: ignore[arg-type]
