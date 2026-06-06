import asyncio
import contextlib
from collections.abc import Callable, Mapping
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import tradedesk.execution.ig.price_streamer as ig_streamer
from tradedesk.execution.ig.price_streamer import StaleStreamError
from tradedesk.marketdata.events import CandleClosedEvent
from tradedesk.marketdata.instrument import MarketData
from tradedesk.marketdata.subscriptions import ChartSubscription, MarketSubscription
from tradedesk.strategy.base import BaseStrategy


class FakeSubscription:
    def __init__(self, mode: str, items: list[str], fields: list[str]) -> None:
        self.mode = mode
        self.items = items
        self.fields = fields
        self._listener: Any = None
        self._data_adapter: str | None = None

    def addListener(self, listener: Any) -> None:
        self._listener = listener

    def setDataAdapter(self, adapter: str) -> None:
        self._data_adapter = adapter


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
        MarketSubscription("CS.D.EURUSD.CFD.IP", account_id="AID"),
        ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
    ]

    async def on_price_update(self, market_data: MarketData) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ig_streamer, "Subscription", FakeSubscription)


@pytest.fixture()
def ig_client() -> MagicMock:
    client = MagicMock()
    client.ls_url = "https://example"
    client.ls_cst = "CST"
    client.ls_xst = "XST"
    client.client_id = "CID"
    client.account_id = "AID"
    # Pre-reconnect re-auth path awaits this — fixture default is a no-op.
    client.auth.authenticate = AsyncMock(return_value=None)
    return client


@pytest.fixture()
def ls_client() -> MagicMock:
    client = MagicMock()
    client.connectionDetails = MagicMock()
    return client


@pytest.fixture()
def patch_ls(
    monkeypatch: pytest.MonkeyPatch,
    ls_client: MagicMock,
) -> MagicMock:
    monkeypatch.setattr(
        ig_streamer,
        "LightstreamerClient",
        lambda *a, **k: ls_client,
    )
    return ls_client


@pytest.fixture()
def subscribed(ls_client: MagicMock) -> list[Any]:
    captured: list[Any] = []
    ls_client.subscribe.side_effect = lambda sub: captured.append(sub)
    return captured


@pytest.fixture()
def make_strategy(
    ig_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., tuple[BaseStrategy, AsyncMock]]:
    def _factory(
        strategy_cls: type[BaseStrategy] = Strategy,
    ) -> tuple[BaseStrategy, AsyncMock]:
        strat = strategy_cls(ig_client)
        handle_event = AsyncMock()
        strat._handle_event = handle_event  # type: ignore[attr-defined]
        return strat, handle_event

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lightstreamer_emits_marketdata_and_candleclose_and_disconnects(
    ig_client: MagicMock,
    ls_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    strat, handle_event = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    assert len(subscribed) == 2
    market_sub = next(s for s in subscribed if s.items[0].startswith("PRICE:"))
    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))

    market_sub._listener.onItemUpdate(
        FakeUpdate(
            item_name="PRICE:AID:CS.D.EURUSD.CFD.IP",
            values={
                "BIDPRICE1": "1.0",
                "ASKPRICE1": "1.1",
                "TIMESTAMP": "1714000000000",
                "DLG_FLAG": "DEAL",
            },
        )
    )

    chart_sub._listener.onItemUpdate(
        FakeUpdate(
            item_name="CHART:CS.D.EURUSD.CFD.IP:5MINUTE",
            values={"CONS_END": "0"},
        )
    )

    chart_sub._listener.onItemUpdate(
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

    await asyncio.sleep(0.05)

    assert handle_event.await_count >= 2
    events = [c.args[0] for c in handle_event.await_args_list]
    assert any(isinstance(e, MarketData) for e in events)
    assert any(isinstance(e, CandleClosedEvent) for e in events)

    task.cancel()
    await task

    ls_client.disconnect.assert_called()
    assert task.done()


@pytest.mark.asyncio
async def test_candle_ohlc_mid_price_values(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Candle OHLC values are the mean of offer and bid prices."""
    strat, handle_event = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))
    chart_sub._listener.onItemUpdate(
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

    events = [c.args[0] for c in handle_event.await_args_list]
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
async def test_malformed_chart_update_missing_close_skipped(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Chart update with missing OFR_CLOSE or BID_CLOSE emits no event."""
    strat, handle_event = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))
    chart_sub._listener.onItemUpdate(
        FakeUpdate(
            item_name="CHART:CS.D.EURUSD.CFD.IP:5MINUTE",
            values={"CONS_END": "1", "OFR_CLOSE": None, "BID_CLOSE": None},
        )
    )

    await asyncio.sleep(0.05)

    assert handle_event.await_count == 0

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_market_update_missing_bid_or_offer_skipped(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Market update with missing BID or OFFER emits no MarketData event."""
    strat, handle_event = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    market_sub = next(s for s in subscribed if s.items[0].startswith("PRICE:"))

    market_sub._listener.onItemUpdate(
        FakeUpdate(
            item_name="PRICE:AID:CS.D.EURUSD.CFD.IP",
            values={"BIDPRICE1": "1.0", "ASKPRICE1": None, "TIMESTAMP": "x", "DLG_FLAG": "DEAL"},
        )
    )
    market_sub._listener.onItemUpdate(
        FakeUpdate(
            item_name="PRICE:AID:CS.D.EURUSD.CFD.IP",
            values={"BIDPRICE1": None, "ASKPRICE1": None, "TIMESTAMP": "x", "DLG_FLAG": "DEAL"},
        )
    )

    await asyncio.sleep(0.05)

    assert handle_event.await_count == 0

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_multiple_chart_subscriptions_route_independently(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Two chart subscriptions (different instruments) each emit their own CandleClosedEvent."""

    class TwoChartStrategy(BaseStrategy):
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
            ChartSubscription("CS.D.USDJPY.CFD.IP", "5MINUTE"),
        ]

        async def on_price_update(self, market_data: MarketData) -> None:
            pass

    strat, handle_event = make_strategy(TwoChartStrategy)
    streamer = ig_streamer.Lightstreamer(ig_client)
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
        sub._listener.onItemUpdate(FakeUpdate(item_name=sub.items[0], values=candle_values))

    await asyncio.sleep(0.05)

    events = [c.args[0] for c in handle_event.await_args_list]
    candle_events = [e for e in events if isinstance(e, CandleClosedEvent)]
    assert len(candle_events) == 2
    instruments = {e.instrument for e in candle_events}
    assert "CS.D.EURUSD.CFD.IP" in instruments
    assert "CS.D.USDJPY.CFD.IP" in instruments

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_connection_status_changes_do_not_crash(
    ig_client: MagicMock,
    ls_client: MagicMock,
    patch_ls: MagicMock,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """ConnectionListener handles status changes and server errors without raising."""
    strat, _ = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
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
async def test_subscription_errors_do_not_crash(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Subscription error callbacks are handled without raising."""
    strat, _ = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    market_sub = next(s for s in subscribed if s.items[0].startswith("PRICE:"))
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
async def test_subscription_error_retries_then_resubscribes(
    ig_client: MagicMock,
    ls_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Subscription errors schedule a delayed resubscribe via the RetryScheduler."""
    schedule_calls: list[tuple[float, Any, str]] = []

    def fake_schedule(
        self: Any, delay: float, action: Any, *, kind: str = "unknown"
    ) -> None:
        schedule_calls.append((delay, action, kind))

    monkeypatch.setattr(
        ig_streamer.RetryScheduler, "schedule", fake_schedule
    )
    # Pin jitter so the first-retry delay equals BASE deterministically.
    monkeypatch.setattr(ig_streamer.random, "uniform", lambda lo, hi: 1.0)

    strat, _ = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    market_sub = next(s for s in subscribed if s.items[0].startswith("PRICE:"))
    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))

    market_sub._listener.onSubscriptionError(21, "Invalid group")
    chart_sub._listener.onSubscriptionError(21, "Invalid group")

    assert len(schedule_calls) == 2
    assert schedule_calls[0][0] == ig_streamer.STREAM_SUB_RETRY_BASE_DELAY_S
    assert schedule_calls[1][0] == ig_streamer.STREAM_SUB_RETRY_BASE_DELAY_S
    assert {c[2] for c in schedule_calls} == {"market", "chart"}

    # Execute the retry callbacks — they should call ls_client.subscribe()
    ls_client.subscribe.reset_mock()
    for _, fn, _kind in schedule_calls:
        fn()
    assert ls_client.subscribe.call_count == 2

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_subscription_error_gives_up_after_max_retries(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After STREAM_SUB_MAX_RETRIES failures, errors are logged at ERROR without further retry."""
    import logging

    schedule_calls: list[Any] = []

    def fake_schedule(
        self: Any, delay: float, action: Any, *, kind: str = "unknown"
    ) -> None:
        schedule_calls.append(action)

    monkeypatch.setattr(
        ig_streamer.RetryScheduler, "schedule", fake_schedule
    )

    strat, _ = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))

    with caplog.at_level(logging.WARNING, logger="tradedesk.execution.ig.price_streamer"):
        for _ in range(ig_streamer.STREAM_SUB_MAX_RETRIES):
            chart_sub._listener.onSubscriptionError(21, "Invalid group")

        assert len(schedule_calls) == ig_streamer.STREAM_SUB_MAX_RETRIES

        schedule_calls.clear()
        chart_sub._listener.onSubscriptionError(21, "Invalid group")
        assert len(schedule_calls) == 0

    assert any("retries exhausted" in r.message for r in caplog.records)

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_successful_subscription_resets_retry_counter(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """onSubscription() resets the retry counter so future errors can retry again."""
    schedule_calls: list[Any] = []

    def fake_schedule(
        self: Any, delay: float, action: Any, *, kind: str = "unknown"
    ) -> None:
        schedule_calls.append(action)

    monkeypatch.setattr(
        ig_streamer.RetryScheduler, "schedule", fake_schedule
    )

    strat, _ = make_strategy()
    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    chart_sub = next(s for s in subscribed if s.items[0].startswith("CHART:"))

    # Exhaust retries
    for _ in range(ig_streamer.STREAM_SUB_MAX_RETRIES):
        chart_sub._listener.onSubscriptionError(21, "Invalid group")

    # Successful subscription resets counter
    chart_sub._listener.onSubscription()

    schedule_calls.clear()
    chart_sub._listener.onSubscriptionError(21, "Invalid group")
    assert len(schedule_calls) == 1  # retry is available again

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_heartbeat_monitor_warns_on_stale_connection(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Heartbeat monitor emits a warning when no updates arrive within the threshold."""
    import logging
    from datetime import datetime, timedelta, timezone

    strat, _ = make_strategy()
    strat.last_update = datetime.now(timezone.utc) - timedelta(seconds=120)
    strat.watchdog_threshold = 60.0

    streamer = ig_streamer.Lightstreamer(ig_client, max_stale_seconds=0)
    streamer.heartbeat_sleep = 0

    with caplog.at_level(logging.WARNING, logger="tradedesk.execution.ig.price_streamer"):
        task = asyncio.create_task(streamer.run(strat))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    assert any("Heartbeat Alert" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_heartbeat_suppresses_after_prolonged_silence(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After prolonged silence (>5 min), heartbeat suppresses repeated warnings."""
    import logging
    from datetime import datetime, timezone

    strat, _ = make_strategy()
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 60.0

    streamer = ig_streamer.Lightstreamer(ig_client, max_stale_seconds=0)
    streamer.heartbeat_sleep = 0

    with caplog.at_level(logging.WARNING, logger="tradedesk.execution.ig.price_streamer"):
        task = asyncio.create_task(streamer.run(strat))
        await asyncio.sleep(0.05)
        task.cancel()
        await task

    assert any("Stream silent" in r.message for r in caplog.records)
    assert not any("Heartbeat Alert" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_heartbeat_threshold_tuned_for_chart_only(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    subscribed: list[Any],
) -> None:
    class ChartOnlyStrategy(BaseStrategy):
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
        ]

        async def on_price_update(self, market_data: MarketData) -> None:
            pass

    strat = ChartOnlyStrategy(ig_client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]
    assert strat.watchdog_threshold == 60

    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    assert strat.watchdog_threshold >= 300

    task.cancel()
    await task


@pytest.mark.asyncio
async def test_stale_stream_reconnects_automatically(
    ig_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Streamer disconnects and reconnects when staleness exceeds max_stale_seconds."""
    from datetime import datetime, timezone

    ls_clients: list[MagicMock] = []

    def make_ls_client(*a: Any, **k: Any) -> MagicMock:
        c = MagicMock()
        c.connectionDetails = MagicMock()
        ls_clients.append(c)
        return c

    monkeypatch.setattr(ig_streamer, "LightstreamerClient", make_ls_client)

    strat, _ = make_strategy()
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 0.01

    # The initial session stales immediately (ancient last_update). Each
    # reconnect session resets the staleness baseline (RAD-3730 td#2), so a
    # short max_stale lets it re-stale promptly without spinning instantly.
    streamer = ig_streamer.Lightstreamer(
        ig_client,
        max_stale_seconds=0.02,
        max_reconnect_attempts=3,
        reconnect_delay=0,
    )
    streamer.heartbeat_sleep = 0

    with pytest.raises(StaleStreamError):
        await streamer.run(strat)

    assert len(ls_clients) == 3
    for c in ls_clients:
        c.disconnect.assert_called()


@pytest.mark.asyncio
async def test_stale_stream_raises_when_auto_reconnect_disabled(
    ig_client: MagicMock,
    ls_client: MagicMock,
    patch_ls: MagicMock,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """StaleStreamError propagates when auto_reconnect is False."""
    from datetime import datetime, timezone

    strat, _ = make_strategy()
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 1.0

    streamer = ig_streamer.Lightstreamer(
        ig_client, max_stale_seconds=2.0, auto_reconnect=False,
    )
    streamer.heartbeat_sleep = 0

    with pytest.raises(StaleStreamError):
        await streamer.run(strat)

    ls_client.disconnect.assert_called()


@pytest.mark.asyncio
async def test_stale_stream_reconnects_unlimited_when_max_zero(
    ig_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """max_reconnect_attempts=0 means unlimited retries; streamer keeps reconnecting."""
    from datetime import datetime, timezone

    connect_count = 0

    def make_ls_client(*a: Any, **k: Any) -> MagicMock:
        nonlocal connect_count
        connect_count += 1
        c = MagicMock()
        c.connectionDetails = MagicMock()
        return c

    monkeypatch.setattr(ig_streamer, "LightstreamerClient", make_ls_client)

    strat, _ = make_strategy()
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 0.01

    # Each reconnect session resets the staleness baseline (RAD-3730 td#2);
    # a short max_stale lets every session re-stale promptly so the unbounded
    # reconnect loop still produces several reconnects within the time window.
    streamer = ig_streamer.Lightstreamer(
        ig_client,
        max_stale_seconds=0.02,
        max_reconnect_attempts=0,
        reconnect_delay=0,
    )
    streamer.heartbeat_sleep = 0

    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.15)
    assert connect_count >= 3

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reconnect_reestablishes_subscriptions(
    ig_client: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """After reconnect, the streamer creates fresh LS client and subscriptions."""
    from datetime import datetime, timezone

    connect_calls: list[MagicMock] = []
    subscribe_calls: list[Any] = []

    def make_ls_client(*a: Any, **k: Any) -> MagicMock:
        c = MagicMock()
        c.connectionDetails = MagicMock()
        c.subscribe.side_effect = lambda sub: subscribe_calls.append(sub)
        connect_calls.append(c)
        return c

    monkeypatch.setattr(ig_streamer, "LightstreamerClient", make_ls_client)

    strat, _ = make_strategy()
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 0.01

    # Each reconnect session resets the staleness baseline (RAD-3730 td#2);
    # a short max_stale lets the reconnect session re-stale promptly.
    streamer = ig_streamer.Lightstreamer(
        ig_client,
        max_stale_seconds=0.02,
        max_reconnect_attempts=2,
        reconnect_delay=0,
    )
    streamer.heartbeat_sleep = 0

    with pytest.raises(StaleStreamError):
        await streamer.run(strat)

    assert len(connect_calls) == 2
    assert len(subscribe_calls) == 4
    connect_calls[0].disconnect.assert_called()
    for c in connect_calls:
        c.connect.assert_called_once()


@pytest.mark.asyncio
async def test_stale_stream_disabled_when_max_stale_zero(
    ig_client: MagicMock,
    patch_ls: MagicMock,
    make_strategy: Callable[..., tuple[BaseStrategy, AsyncMock]],
) -> None:
    """Setting max_stale_seconds=0 disables the reconnect watchdog (warns only)."""
    from datetime import datetime, timezone

    strat, _ = make_strategy()
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)
    strat.watchdog_threshold = 1.0

    streamer = ig_streamer.Lightstreamer(ig_client, max_stale_seconds=0)
    streamer.heartbeat_sleep = 0

    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)
    assert not task.done()

    task.cancel()
    await task
