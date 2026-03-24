import asyncio
from collections.abc import Mapping
from unittest.mock import AsyncMock, MagicMock

import pytest

import tradedesk.execution.ig.price_streamer as ig_streamer
from tradedesk.marketdata.events import CandleClosedEvent
from tradedesk.marketdata.instrument import MarketData
from tradedesk.marketdata.subscriptions import ChartSubscription, MarketSubscription
from tradedesk.strategy.base import BaseStrategy


class FakeSubscription:
    def __init__(self, mode, items, fields):
        self.mode = mode
        self.items = items
        self.fields = fields
        self._listener = None

    def addListener(self, listener):
        self._listener = listener


class FakeUpdate:
    def __init__(self, item_name: str, values: Mapping[str, str | None]) -> None:
        self._item_name = item_name
        self._values = values

    def getItemName(self) -> str:
        return self._item_name

    def getValue(self, key: str) -> str | None:
        return self._values.get(key)


class Strategy(BaseStrategy):
    SUBSCRIPTIONS = [
        MarketSubscription("CS.D.EURUSD.CFD.IP"),
        ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
    ]

    async def on_price_update(self, instrument, bid, offer, timestamp, raw_data):
        pass


@pytest.mark.asyncio
async def test_lightstreamer_emits_marketdata_and_candleclose_and_disconnects():
    # Patch Subscription class used by streamer
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]

    # Build a fake LS client instance
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()

    # Capture subscriptions passed to subscribe()
    subscribed = []

    def subscribe(sub):
        subscribed.append(sub)

    ls_client.subscribe.side_effect = subscribe

    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    # Strategy + client stub
    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

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
    assert any(isinstance(e, CandleClosedEvent) for e in events)

    # Cancel and ensure disconnect called
    task.cancel()
    await task  # run() may swallow CancelledError and exit cleanly

    ls_client.disconnect.assert_called()
    assert task.done()


@pytest.mark.asyncio
async def test_candle_ohlc_mid_price_values() -> None:
    """Candle OHLC values are the mean of offer and bid prices."""
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    subscribed = []
    ls_client.subscribe.side_effect = lambda sub: subscribed.append(sub)

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))
    chart_listener = chart_sub._listener
    chart_listener.onItemUpdate(
        FakeUpdate(
            item_name="CHART:CS.D.EURUSD.CFD.IP:5MINUTE",
            values={
                "CONS_END": "1",
                "UTM": "2025-12-28T00:00:00Z",
                "OFR_OPEN": "1.100",
                "OFR_HIGH": "1.200",
                "OFR_LOW": "0.900",
                "OFR_CLOSE": "1.050",
                "BID_OPEN": "1.090",
                "BID_HIGH": "1.190",
                "BID_LOW": "0.890",
                "BID_CLOSE": "1.040",
                "LTV": "5",
                "CONS_TICK_COUNT": "2",
            },
        )
    )

    await asyncio.sleep(0.05)

    events = [c.args[0] for c in strat._handle_event.await_args_list]  # type: ignore[attr-defined]
    candle_events = [e for e in events if isinstance(e, CandleClosedEvent)]
    assert len(candle_events) == 1
    c = candle_events[0].candle
    assert abs(c.open - (1.100 + 1.090) / 2) < 1e-9
    assert abs(c.high - (1.200 + 1.190) / 2) < 1e-9
    assert abs(c.low - (0.900 + 0.890) / 2) < 1e-9
    assert abs(c.close - (1.050 + 1.040) / 2) < 1e-9
    assert c.volume == 5.0
    assert c.tick_count == 2

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_malformed_chart_update_missing_close_skipped() -> None:
    """Chart update with missing OFR_CLOSE or BID_CLOSE emits no event."""
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    subscribed = []
    ls_client.subscribe.side_effect = lambda sub: subscribed.append(sub)

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))
    chart_listener = chart_sub._listener
    # Missing OFR_CLOSE and BID_CLOSE
    chart_listener.onItemUpdate(
        FakeUpdate(
            item_name="CHART:CS.D.EURUSD.CFD.IP:5MINUTE",
            values={"CONS_END": "1", "OFR_CLOSE": None, "BID_CLOSE": None},
        )
    )

    await asyncio.sleep(0.05)

    assert strat._handle_event.await_count == 0  # type: ignore[attr-defined]

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_market_update_missing_bid_or_offer_skipped() -> None:
    """Market update with missing BID or OFFER emits no MarketData event."""
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    subscribed = []
    ls_client.subscribe.side_effect = lambda sub: subscribed.append(sub)

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    market_sub = next(s for s in subscribed if s.items[0].startswith("MARKET:"))
    market_listener = market_sub._listener

    # BID present, OFFER missing
    market_listener.onItemUpdate(
        FakeUpdate(
            item_name="MARKET:CS.D.EURUSD.CFD.IP",
            values={"BID": "1.0", "OFFER": None, "UPDATE_TIME": "x", "MARKET_STATE": "TRADEABLE"},
        )
    )
    # Both missing
    market_listener.onItemUpdate(
        FakeUpdate(
            item_name="MARKET:CS.D.EURUSD.CFD.IP",
            values={"BID": None, "OFFER": None, "UPDATE_TIME": "x", "MARKET_STATE": "TRADEABLE"},
        )
    )

    await asyncio.sleep(0.05)

    assert strat._handle_event.await_count == 0  # type: ignore[attr-defined]

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_multiple_chart_subscriptions_route_independently() -> None:
    """Two chart subscriptions (different instruments) each emit their own CandleClosedEvent."""
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    class TwoChartStrategy(BaseStrategy):
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
            ChartSubscription("CS.D.USDJPY.CFD.IP", "5MINUTE"),
        ]

        async def on_price_update(self, market_data: MarketData) -> None:
            pass

    strat = TwoChartStrategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    subscribed = []
    ls_client.subscribe.side_effect = lambda sub: subscribed.append(sub)

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_subs = [s for s in subscribed if s.items[0].startswith("CHART:")]
    assert len(chart_subs) == 2

    candle_values = {
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
        "LTV": "1",
        "CONS_TICK_COUNT": "1",
    }

    for sub in chart_subs:
        instrument = sub.items[0].split(":")[1]
        sub._listener.onItemUpdate(FakeUpdate(item_name=sub.items[0], values=candle_values))
        _ = instrument  # used for clarity

    await asyncio.sleep(0.05)

    events = [c.args[0] for c in strat._handle_event.await_args_list]  # type: ignore[attr-defined]
    candle_events = [e for e in events if isinstance(e, CandleClosedEvent)]
    assert len(candle_events) == 2
    instruments = {e.instrument for e in candle_events}
    assert "CS.D.EURUSD.CFD.IP" in instruments
    assert "CS.D.USDJPY.CFD.IP" in instruments

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_connection_status_changes_do_not_crash() -> None:
    """ConnectionListener handles status changes and server errors without raising."""
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    conn_listener = ls_client.addListener.call_args[0][0]
    conn_listener.onStatusChange("CONNECTED:WS-STREAMING")
    conn_listener.onStatusChange("DISCONNECTED:WILL-RETRY")
    conn_listener.onStatusChange("CONNECTED:WS-STREAMING")
    conn_listener.onServerError(42, "Server error")

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_subscription_errors_do_not_crash() -> None:
    """Subscription error callbacks are handled without raising."""
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    subscribed = []
    ls_client.subscribe.side_effect = lambda sub: subscribed.append(sub)

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    market_sub = next(s for s in subscribed if s.items[0].startswith("MARKET:"))
    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))

    market_sub._listener.onSubscriptionError(503, "Service unavailable")
    chart_sub._listener.onSubscriptionError(503, "Service unavailable")
    market_sub._listener.onSubscription()
    chart_sub._listener.onSubscription()
    market_sub._listener.onUnsubscription()
    chart_sub._listener.onUnsubscription()

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_heartbeat_monitor_warns_on_stale_connection(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Heartbeat monitor emits a warning when no updates arrive within the threshold."""
    import logging
    from datetime import datetime, timezone

    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    strat = Strategy(client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]
    # Backdate last_update far enough to trigger the watchdog
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 60.0

    streamer = ig_streamer.Lightstreamer(client)
    streamer.heartbeat_sleep = 0  # fast loop for test

    with caplog.at_level(logging.WARNING, logger="tradedesk.execution.ig.price_streamer"):
        task = asyncio.create_task(streamer.run(strat))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    assert any("Heartbeat Alert" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_heartbeat_threshold_tuned_for_chart_only() -> None:
    # Patch Subscription class used by streamer
    ig_streamer.Subscription = FakeSubscription  # type: ignore[attr-defined]

    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()

    subscribed = []
    ls_client.subscribe.side_effect = lambda sub: subscribed.append(sub)

    ig_streamer.LightstreamerClient = lambda *a, **k: ls_client  # type: ignore[attr-defined]

    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"

    class ChartOnlyStrategy(BaseStrategy):
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
        ]

        async def on_price_update(self, market_data: MarketData) -> None:
            pass

    strat = ChartOnlyStrategy(client)
    assert strat.watchdog_threshold == 60

    streamer = ig_streamer.Lightstreamer(client)
    task = asyncio.create_task(streamer.run(strat))

    await asyncio.sleep(0.05)

    # For chart-only streams, threshold should be raised to at least one bar (5 minutes)
    assert strat.watchdog_threshold >= 300

    task.cancel()
    await task
