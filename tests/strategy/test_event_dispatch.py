from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.marketdata.events import CandleClosedEvent
from tradedesk.marketdata.instrument import MarketData
from tradedesk.marketdata.subscriptions import ChartSubscription
from tradedesk.portfolio.simple import SimplePortfolio
from tradedesk.strategy.base import BaseStrategy
from tradedesk.types import Candle


class Strat(BaseStrategy):
    SUBSCRIPTIONS = []

    async def on_price_update(self, md: MarketData):
        pass


def make_portfolio(client=None):
    """Create a SimplePortfolio wrapping a minimal strategy."""
    client = client or MagicMock()
    strategy = Strat(client)
    return SimplePortfolio(client, strategy)


@pytest.mark.asyncio
async def test_handle_event_marketdata_updates_last_update_and_dispatches():
    portfolio = make_portfolio()
    before = portfolio.last_update

    event = MarketData(
        instrument="EPIC",
        bid=1.0,
        offer=1.1,
        timestamp="2025-12-28T00:00:00Z",
        raw={"foo": "bar"},
    )

    await portfolio._handle_event(event)

    assert portfolio.last_update >= before


@pytest.mark.asyncio
async def test_handle_event_candleclose_dispatches_and_uses_default_storage():
    class S(BaseStrategy):
        SUBSCRIPTIONS = [ChartSubscription("EPIC", "5MINUTE")]

        async def on_price_update(self, market_data):
            pass

    client = MagicMock()
    strategy = S(client)
    portfolio = SimplePortfolio(client, strategy)

    candle = Candle(
        timestamp="2025-12-28T00:00:00Z",
        open=1.0,
        high=1.2,
        low=0.9,
        close=1.1,
        volume=10.0,
        tick_count=3,
    )
    event = CandleClosedEvent(instrument="EPIC", timeframe="5MINUTE", candle=candle)

    await portfolio._handle_event(event)

    # The default on_candle_close in BaseStrategy stores the candle in chart history
    key = ("EPIC", "5MINUTE")
    assert key in strategy.charts
    assert strategy.charts[key].get_candles()[-1] == candle


@pytest.mark.asyncio
async def test_handle_event_rejects_unknown_type():
    portfolio = make_portfolio()
    with pytest.raises(TypeError):
        await portfolio._handle_event(object())  # type: ignore[arg-type]
