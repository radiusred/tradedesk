import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from tradedesk.marketdata import MarketData
import tradedesk.providers.ig.streamer as ig_streamer
from tradedesk.marketdata import CandleClose
from tradedesk.subscriptions import MarketSubscription, ChartSubscription
from tradedesk.strategy import BaseStrategy


class FakeSubscription:
    def __init__(self, mode, items, fields):
        self.mode = mode
        self.items = items
        self.fields = fields
        self._listener = None

    def addListener(self, listener):
        self._listener = listener


class FakeUpdate:
    def __init__(self, item_name, values):
        self._item_name = item_name
        self._values = values

    def getItemName(self):
        return self._item_name

    def getValue(self, key):
        return self._values.get(key)


class Strategy(BaseStrategy):
    SUBSCRIPTIONS = [
        MarketSubscription("CS.D.EURUSD.CFD.IP"),
        ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
    ]

    async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
        pass


@pytest.mark.asyncio
async def test_lightstreamer_emits_marketdata_and_candleclose_and_disconnects():
    # Patch Subscription class used by streamer
    ig_streamer.Subscription = FakeSubscription  # type: ignore[assignment]

    # Build a fake LS client instance
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()

    # Capture subscriptions passed to subscribe()
    subscribed = []

    def subscribe(sub):
        subscribed.append(sub)

    ls_client.subscribe.side_effect = subscribe

    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[assignment]

    # Strategy + client stub
    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[method-assign]

    streamer = ig_streamer.Lightstreamer(client)

    task = asyncio.create_task(streamer.run(strat))

    # Allow the streamer to connect + subscribe
    await asyncio.sleep(0.05)

    # We expect 1 market subscription and 1 chart subscription to have been created and subscribed
    assert len(subscribed) == 2
    market_sub = next(s for s in subscribed if s.items[0].startswith("MARKET:"))
    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))

    # Emit a market tick
    market_listener = market_sub._listener
    market_listener.onItemUpdate(
        FakeUpdate(
            item_name="MARKET:CS.D.EURUSD.CFD.IP",
            values={
                "BID": "1.0",
                "OFFER": "1.1",
                "UPDATE_TIME": "x",
                "MARKET_STATE": "TRADEABLE",
            },
        )
    )

    # Emit an incomplete candle (ignored)
    chart_listener = chart_sub._listener
    chart_listener.onItemUpdate(
        FakeUpdate(
            item_name="CHART:CS.D.EURUSD.CFD.IP:5MINUTE",
            values={"CONS_END": "0"},
        )
    )

    # Emit a completed candle
    chart_listener.onItemUpdate(
        FakeUpdate(
            item_name="CHART:CS.D.EURUSD.CFD.IP:5MINUTE",
            values={
                "CONS_END": "1",
                "UTM": "2025-12-28T00:00:00Z",
                "OFR_OPEN": "1.0",
                "OFR_HIGH": "1.2",
                "OFR_LOW": "0.9",
                "OFR_CLOSE": "1.1",
                "BID_OPEN": "0.99",
                "BID_HIGH": "1.19",
                "BID_LOW": "0.89",
                "BID_CLOSE": "1.09",
                "LTV": "10",
                "CONS_TICK_COUNT": "3",
            },
        )
    )

    # Give consumers time to process
    await asyncio.sleep(0.05)

    # Verify _handle_event got called for market and candle
    assert strat._handle_event.await_count >= 2  # type: ignore[attr-defined]
    events = [c.args[0] for c in strat._handle_event.await_args_list]  # type: ignore[attr-defined]
    assert any(isinstance(e, MarketData) for e in events)
    assert any(isinstance(e, CandleClose) for e in events)

    # Cancel and ensure disconnect called
    task.cancel()
    await task  # run() may swallow CancelledError and exit cleanly

    ls_client.disconnect.assert_called()
    assert task.done()
