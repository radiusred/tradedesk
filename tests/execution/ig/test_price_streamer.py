"""Unit tests for tradedesk.execution.ig.price_streamer

Covers:
  - RetryScheduler: scheduling, cancellation, and closed-state semantics
  - RetryScheduler._run: metric increment on action invocation
  - _subscription_retry_delay: exponential schedule with cap and full jitter
  - _MarketListener.onSubscriptionError: jittered exponential backoff +
    max-retries cutoff
  - Subscription retry action: calls ls_client.subscribe()
  - _MarketListener.onItemUpdate exception path: logged with traceback, tick dropped
  - _ChartListener.onItemUpdate exception path: logged with traceback, tick dropped
  - _heartbeat_monitor: suppression then resume logging
"""

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import tradedesk.execution.ig.price_streamer as streamer_mod
from tradedesk.execution.ig.price_streamer import RetryScheduler, _ChartListener, _MarketListener
from tradedesk.marketdata import ChartSubscription, MarketSubscription
from tradedesk.strategy.base import BaseStrategy

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _make_market_listener(
    loop: asyncio.AbstractEventLoop | None = None,
    scheduler: Any = None,
    ls_client: Any = None,
) -> _MarketListener:
    if loop is None:
        loop = MagicMock()
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    if ls_client is None:
        ls_client = MagicMock()
    ls_sub = MagicMock()
    if scheduler is None:
        scheduler = MagicMock()
    return _MarketListener(
        loop=loop,
        queue=queue,
        items=["PRICE:AID:CS.D.EURUSD.CFD.IP"],
        ls_client=ls_client,
        ls_subscription=ls_sub,
        scheduler=scheduler,
    )


# ──────────────────────────────────────────────────────────────────────────────
# RetryScheduler
# ──────────────────────────────────────────────────────────────────────────────


async def test_retry_scheduler_action_invoked_after_delay() -> None:
    """schedule() runs the action after the given delay on the running loop."""
    loop = asyncio.get_running_loop()
    sched = RetryScheduler(loop)
    called: list[bool] = []

    sched.schedule(0, lambda: called.append(True), kind="test")
    await asyncio.sleep(0.05)

    assert called == [True]
    await sched.cancel_all()


async def test_retry_scheduler_cancel_all_prevents_invocation() -> None:
    """cancel_all() cancels pending tasks before they fire."""
    loop = asyncio.get_running_loop()
    sched = RetryScheduler(loop)
    called: list[bool] = []

    sched.schedule(10.0, lambda: called.append(True), kind="test")
    await asyncio.sleep(0)  # Let _spawn register the task
    await sched.cancel_all()

    assert called == []


async def test_retry_scheduler_closed_ignores_new_schedules() -> None:
    """After cancel_all(), further schedule() calls are silently dropped."""
    loop = asyncio.get_running_loop()
    sched = RetryScheduler(loop)
    await sched.cancel_all()  # Mark closed

    called: list[bool] = []
    sched.schedule(0, lambda: called.append(True), kind="test")
    await asyncio.sleep(0.05)

    assert called == []


async def test_retry_scheduler_increments_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    """_run increments SUBSCRIPTION_RETRIES.labels(kind=...) when the action fires."""
    loop = asyncio.get_running_loop()
    sched = RetryScheduler(loop)

    counter_mock = MagicMock()
    monkeypatch.setattr(streamer_mod, "SUBSCRIPTION_RETRIES", counter_mock)

    sched.schedule(0, lambda: None, kind="market")
    await asyncio.sleep(0.05)

    counter_mock.labels.assert_called_once_with(kind="market")
    counter_mock.labels.return_value.inc.assert_called_once()
    await sched.cancel_all()


# ──────────────────────────────────────────────────────────────────────────────
# _subscription_retry_delay — jittered exponential backoff
# ──────────────────────────────────────────────────────────────────────────────


def test_subscription_retry_delay_jittered_exponential_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delay grows as ``min(BASE * 2**retry, MAX) * jitter`` with full jitter.

    Pins ``random.uniform`` so the schedule is deterministic, then walks
    enough retries to cross the cap, verifying both the exponential growth
    region and the saturated region.
    """
    monkeypatch.setattr(streamer_mod, "STREAM_SUB_RETRY_BASE_DELAY_S", 2.0)
    monkeypatch.setattr(streamer_mod, "STREAM_SUB_RETRY_MAX_DELAY_S", 30.0)

    jitter_calls: list[tuple[float, float]] = []

    def fake_uniform(lo: float, hi: float) -> float:
        jitter_calls.append((lo, hi))
        return 1.0

    monkeypatch.setattr(streamer_mod.random, "uniform", fake_uniform)

    # Exponential region: 2, 4, 8, 16 (all ≤ MAX=30)
    assert streamer_mod._subscription_retry_delay(0) == pytest.approx(2.0)
    assert streamer_mod._subscription_retry_delay(1) == pytest.approx(4.0)
    assert streamer_mod._subscription_retry_delay(2) == pytest.approx(8.0)
    assert streamer_mod._subscription_retry_delay(3) == pytest.approx(16.0)

    # Cap region: 32 → 30, 64 → 30, 128 → 30
    assert streamer_mod._subscription_retry_delay(4) == pytest.approx(30.0)
    assert streamer_mod._subscription_retry_delay(5) == pytest.approx(30.0)
    assert streamer_mod._subscription_retry_delay(10) == pytest.approx(30.0)

    # Full jitter spans the documented [0.5, 1.5) range every call
    assert jitter_calls == [(0.5, 1.5)] * 7


def test_subscription_retry_delay_applies_jitter_multiplier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jitter multiplier is applied to the capped exponential term."""
    monkeypatch.setattr(streamer_mod, "STREAM_SUB_RETRY_BASE_DELAY_S", 2.0)
    monkeypatch.setattr(streamer_mod, "STREAM_SUB_RETRY_MAX_DELAY_S", 30.0)

    monkeypatch.setattr(streamer_mod.random, "uniform", lambda lo, hi: 0.5)
    assert streamer_mod._subscription_retry_delay(2) == pytest.approx(8.0 * 0.5)

    monkeypatch.setattr(streamer_mod.random, "uniform", lambda lo, hi: 1.5)
    assert streamer_mod._subscription_retry_delay(2) == pytest.approx(8.0 * 1.5)


# ──────────────────────────────────────────────────────────────────────────────
# _MarketListener — subscription retry path
# ──────────────────────────────────────────────────────────────────────────────


def test_market_subscription_error_schedules_with_jittered_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """onSubscriptionError schedules retries via the jittered backoff helper."""
    schedule_calls: list[tuple[float, Any, str]] = []

    def fake_schedule(
        self: Any, delay: float, action: Any, *, kind: str = "unknown"
    ) -> None:
        schedule_calls.append((delay, action, kind))

    monkeypatch.setattr(RetryScheduler, "schedule", fake_schedule)
    monkeypatch.setattr(streamer_mod, "STREAM_SUB_RETRY_BASE_DELAY_S", 2.0)
    monkeypatch.setattr(streamer_mod, "STREAM_SUB_RETRY_MAX_DELAY_S", 30.0)
    monkeypatch.setattr(streamer_mod.random, "uniform", lambda lo, hi: 1.0)

    sched = RetryScheduler.__new__(RetryScheduler)
    listener = _make_market_listener(scheduler=sched)

    listener.onSubscriptionError(503, "unavailable")
    assert len(schedule_calls) == 1
    assert schedule_calls[0][0] == pytest.approx(2.0)  # BASE * 2**0 * 1.0
    assert schedule_calls[0][2] == "market"

    listener.onSubscriptionError(503, "unavailable")
    assert len(schedule_calls) == 2
    assert schedule_calls[1][0] == pytest.approx(4.0)  # BASE * 2**1 * 1.0

    listener.onSubscriptionError(503, "unavailable")
    assert len(schedule_calls) == 3
    assert schedule_calls[2][0] == pytest.approx(8.0)  # BASE * 2**2 * 1.0


def test_market_subscription_error_stops_after_max_retries(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After STREAM_SUB_MAX_RETRIES errors, no further retries are scheduled."""
    schedule_calls: list[Any] = []

    def fake_schedule(
        self: Any, delay: float, action: Any, *, kind: str = "unknown"
    ) -> None:
        schedule_calls.append(action)

    monkeypatch.setattr(RetryScheduler, "schedule", fake_schedule)

    sched = RetryScheduler.__new__(RetryScheduler)
    listener = _make_market_listener(scheduler=sched)
    max_retries = streamer_mod.STREAM_SUB_MAX_RETRIES

    with caplog.at_level(logging.ERROR, logger="tradedesk.execution.ig.price_streamer"):
        for _ in range(max_retries):
            listener.onSubscriptionError(503, "retry")
        assert len(schedule_calls) == max_retries

        schedule_calls.clear()
        listener.onSubscriptionError(503, "exhausted")
        assert len(schedule_calls) == 0

    assert any("retries exhausted" in r.message for r in caplog.records)


def test_subscription_retry_action_calls_ls_subscribe() -> None:
    """The retry callback produced by onSubscriptionError calls ls_client.subscribe()."""
    ls_client = MagicMock()
    schedule_calls: list[tuple[float, Any]] = []

    sched = MagicMock(spec=RetryScheduler)
    sched.schedule.side_effect = lambda delay, action, *, kind="unknown": schedule_calls.append(
        (delay, action)
    )

    listener = _make_market_listener(scheduler=sched, ls_client=ls_client)
    listener.onSubscriptionError(21, "Invalid group")

    assert len(schedule_calls) == 1
    _, retry_fn = schedule_calls[0]

    ls_client.subscribe.reset_mock()
    retry_fn()
    ls_client.subscribe.assert_called_once()


# ──────────────────────────────────────────────────────────────────────────────
# onItemUpdate exception path
# ──────────────────────────────────────────────────────────────────────────────


def test_market_onItemUpdate_exception_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When onItemUpdate raises internally, error is logged with exc_info and tick is dropped."""
    bad_update = MagicMock()
    bad_update.getValue.side_effect = AttributeError("parse failure")

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    listener = _MarketListener(
        loop=MagicMock(),
        queue=queue,
        items=["PRICE:AID:CS.D.EURUSD.CFD.IP"],
        ls_client=MagicMock(),
        ls_subscription=MagicMock(),
        scheduler=MagicMock(),
    )

    with caplog.at_level(logging.ERROR, logger="tradedesk.execution.ig.price_streamer"):
        listener.onItemUpdate(bad_update)  # Must not raise

    assert queue.empty()
    assert any(r.exc_info for r in caplog.records)


def test_chart_onItemUpdate_exception_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chart onItemUpdate swallows parse errors, logs with exc_info, and drops the tick."""
    bad_update = MagicMock()
    bad_update.getValue.side_effect = ValueError("bad value")

    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    sub = ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE")
    listener = _ChartListener(
        loop=MagicMock(),
        queue=queue,
        sub=sub,
        ls_client=MagicMock(),
        ls_subscription=MagicMock(),
        scheduler=MagicMock(),
    )

    with caplog.at_level(logging.ERROR, logger="tradedesk.execution.ig.price_streamer"):
        listener.onItemUpdate(bad_update)  # Must not raise

    assert queue.empty()
    assert any(r.exc_info for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────────────
# Heartbeat: suppression then resume
# ──────────────────────────────────────────────────────────────────────────────


async def test_heartbeat_logs_resumed_after_data_resumes(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """After entering suppressed state, heartbeat logs 'resumed' when data comes back."""
    from datetime import datetime, timezone

    monkeypatch.setattr(streamer_mod, "STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S", 0)
    monkeypatch.setattr(streamer_mod, "STREAM_SILENCE_SUPPRESS_THRESHOLD_S", 60.0)

    ig_client = MagicMock()
    ig_client.ls_url = "https://example"
    ig_client.ls_cst = "CST"
    ig_client.ls_xst = "XST"
    ig_client.client_id = "CID"
    ig_client.account_id = "AID"

    monkeypatch.setattr(
        streamer_mod,
        "LightstreamerClient",
        lambda *a, **k: MagicMock(connectionDetails=MagicMock()),
    )
    monkeypatch.setattr(streamer_mod, "Subscription", MagicMock())

    class _MinStrat(BaseStrategy):
        SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP", account_id="AID")]

        async def on_price_update(self, md: Any) -> None:
            pass

    strat = _MinStrat(ig_client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]
    strat.last_update = datetime(2020, 1, 1, tzinfo=timezone.utc)  # very old
    strat.watchdog_threshold = 30.0

    s = streamer_mod.Lightstreamer(ig_client, max_stale_seconds=0)
    s.heartbeat_sleep = 0

    with caplog.at_level(logging.INFO, logger="tradedesk.execution.ig.price_streamer"):
        task = asyncio.create_task(s.run(strat))
        await asyncio.sleep(0.05)  # Triggers suppressed state

        strat.last_update = datetime.now(timezone.utc)  # Simulate data resuming
        await asyncio.sleep(0.05)  # Heartbeat loops fast (suppressed sleep = 0)

        task.cancel()
        await task

    messages = [r.message for r in caplog.records]
    assert any("silent" in m.lower() or "Suppressing" in m for m in messages)
    assert any("resumed" in m.lower() for m in messages)


# ──────────────────────────────────────────────────────────────────────────────
# Session-state bumping by listeners
# ──────────────────────────────────────────────────────────────────────────────


def test_market_listener_bumps_session_state_on_each_update() -> None:
    """Every onItemUpdate call increments session_state.bars_received."""
    state = streamer_mod._SessionState()
    listener = _MarketListener(
        loop=MagicMock(),
        queue=asyncio.Queue(),
        items=["PRICE:AID:CS.D.EURUSD.CFD.IP"],
        ls_client=MagicMock(),
        ls_subscription=MagicMock(),
        scheduler=MagicMock(),
        session_state=state,
    )
    update = MagicMock()
    update.getValue.side_effect = lambda f: {
        "BIDPRICE1": "1.10000",
        "ASKPRICE1": "1.10002",
        "TIMESTAMP": "x",
        "DLG_FLAG": "0",
    }.get(f)
    update.getItemName.return_value = "PRICE:AID:CS.D.EURUSD.CFD.IP"

    listener.onItemUpdate(update)
    listener.onItemUpdate(update)

    assert state.bars_received == 2


def test_chart_listener_bumps_session_state_on_each_update() -> None:
    """_ChartListener bumps session_state on every onItemUpdate (even partial bars)."""
    state = streamer_mod._SessionState()
    sub = ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE")
    listener = _ChartListener(
        loop=MagicMock(),
        queue=asyncio.Queue(),
        sub=sub,
        ls_client=MagicMock(),
        ls_subscription=MagicMock(),
        scheduler=MagicMock(),
        session_state=state,
    )
    # CONS_END != "1" — partial bar — still counts as data flowing
    update = MagicMock()
    update.getValue.return_value = "0"

    listener.onItemUpdate(update)
    listener.onItemUpdate(update)
    listener.onItemUpdate(update)

    assert state.bars_received == 3


# ──────────────────────────────────────────────────────────────────────────────
# _ConnectionListener — connected-event signalling
# ──────────────────────────────────────────────────────────────────────────────


def test_connection_listener_fires_on_connected_for_connected_status() -> None:
    """on_connected callback is scheduled via the loop for any CONNECTED:* status."""
    fired: list[bool] = []
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = lambda cb: fired.append(True) or cb()

    listener = streamer_mod._ConnectionListener(
        loop=loop, on_connected=lambda: None,
    )
    listener.onStatusChange("CONNECTED:WS-STREAMING")
    assert fired == [True]

    listener.onStatusChange("CONNECTED:HTTP-POLLING")
    assert fired == [True, True]


def test_connection_listener_ignores_non_connected_status() -> None:
    """Statuses like CONNECTING / STALLED / DISCONNECTED don't fire on_connected."""
    fired: list[bool] = []
    loop = MagicMock()
    loop.call_soon_threadsafe.side_effect = lambda cb: fired.append(True)

    listener = streamer_mod._ConnectionListener(
        loop=loop, on_connected=lambda: None,
    )
    for status in ("CONNECTING", "STALLED", "DISCONNECTED:WILL-RETRY"):
        listener.onStatusChange(status)

    assert fired == []


def test_connection_listener_without_callback_is_noop() -> None:
    """Backwards-compat: no callback / no loop wired ⇒ status changes don't crash."""
    listener = streamer_mod._ConnectionListener()
    listener.onStatusChange("CONNECTED:WS-STREAMING")  # must not raise


# ──────────────────────────────────────────────────────────────────────────────
# _grace_monitor
# ──────────────────────────────────────────────────────────────────────────────


async def test_grace_monitor_raises_unproductive_when_no_bars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If connected fires and grace elapses with bars=0, raise _UnproductiveSession."""
    ig_client = MagicMock()
    s = streamer_mod.Lightstreamer(
        ig_client, unproductive_grace_seconds=0.01,
    )
    state = streamer_mod._SessionState()
    connected = asyncio.Event()
    connected.set()

    with pytest.raises(streamer_mod._UnproductiveSession) as ei:
        await s._grace_monitor(state, connected)
    assert ei.value.bars_received == 0


async def test_grace_monitor_returns_clean_when_bars_arrived() -> None:
    """If bars_received > 0 by the time grace elapses, return cleanly."""
    ig_client = MagicMock()
    s = streamer_mod.Lightstreamer(
        ig_client, unproductive_grace_seconds=0.01,
    )
    state = streamer_mod._SessionState()
    state.bump()  # Simulate one tick arrived
    connected = asyncio.Event()
    connected.set()

    # Must not raise — productive session
    await s._grace_monitor(state, connected)


async def test_grace_monitor_blocks_until_connected() -> None:
    """Grace timer doesn't start until the connected_event fires."""
    ig_client = MagicMock()
    s = streamer_mod.Lightstreamer(
        ig_client, unproductive_grace_seconds=0.01,
    )
    state = streamer_mod._SessionState()
    connected = asyncio.Event()
    # connected is NOT set — monitor should block

    task = asyncio.create_task(s._grace_monitor(state, connected))
    await asyncio.sleep(0.05)
    assert not task.done()  # Still waiting for connected

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# run() — pre-reconnect IG /session re-auth
# ──────────────────────────────────────────────────────────────────────────────


def _stub_ls_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace LightstreamerClient + Subscription with cheap mocks."""
    monkeypatch.setattr(
        streamer_mod, "LightstreamerClient",
        lambda *a, **k: MagicMock(connectionDetails=MagicMock()),
    )
    monkeypatch.setattr(streamer_mod, "Subscription", MagicMock())


async def test_run_reauths_before_recreating_lightstreamer_on_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a reconnect attempt, IG /session is refreshed BEFORE the new LS client is built.

    Token refresh is observed by ensuring the second LS-client construction
    sees the post-reauth CST/XST, not the original ones.
    """
    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_url = "https://lsdemo"
    ig_client.ls_cst = "OLD_CST"
    ig_client.ls_xst = "OLD_XST"
    ig_client.client_id = "CID"
    ig_client.account_id = "AID"

    async def fake_authenticate() -> None:
        ig_client.ls_cst = "NEW_CST"
        ig_client.ls_xst = "NEW_XST"

    ig_client.auth.authenticate = fake_authenticate

    call_log: list[tuple[str, str | None]] = []

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=2,
        unproductive_reconnect_cap=10,
        unproductive_grace_seconds=999,
    )
    s.reconnect_delay = 0

    async def fake_run_session(consumer: Any) -> None:
        call_log.append(("session", ig_client.ls_cst))
        # Pretend the session ran and was productive, then went stale —
        # so unproductive cap doesn't trip and we get a clean reconnect path.
        s._last_session_state = streamer_mod._SessionState()
        s._last_session_state.bars_received = 1
        raise streamer_mod.StaleStreamError("forced")

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    consumer = MagicMock()

    with pytest.raises(streamer_mod.StaleStreamError):
        await s.run(consumer)

    # First session uses original tokens (no reauth on initial attempt);
    # second session sees the NEW tokens populated by fake_authenticate.
    assert call_log == [
        ("session", "OLD_CST"),
        ("session", "NEW_CST"),
    ]


async def test_run_emits_structured_reauth_log_markers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """reauth_attempted / reauth_result / reconnect_attempt structured markers emitted."""
    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        return None

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=2,
        unproductive_reconnect_cap=10,
    )
    s.reconnect_delay = 0

    async def fake_run_session(consumer: Any) -> None:
        s._last_session_state = streamer_mod._SessionState()
        s._last_session_state.bars_received = 1
        raise streamer_mod.StaleStreamError("forced")

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    with caplog.at_level(logging.INFO, logger="tradedesk.execution.ig.price_streamer"):
        with pytest.raises(streamer_mod.StaleStreamError):
            await s.run(MagicMock())

    messages = [r.message for r in caplog.records]
    assert any("reconnect_attempt attempt=1" in m for m in messages)
    assert any("reauth_attempted reason=reconnect attempt=1" in m for m in messages)
    assert any("reauth_result attempt=1 status=ok" in m for m in messages)


# ──────────────────────────────────────────────────────────────────────────────
# run() — unproductive reconnect cap
# ──────────────────────────────────────────────────────────────────────────────


async def test_run_raises_unproductive_error_exactly_after_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N consecutive unproductive sessions → UnproductiveReconnectError; no further attempts."""
    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        return None

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=0,
        unproductive_reconnect_cap=3,
        unproductive_grace_seconds=0.01,
    )
    s.reconnect_delay = 0

    session_count = 0

    async def fake_run_session(consumer: Any) -> None:
        nonlocal session_count
        session_count += 1
        s._last_session_state = streamer_mod._SessionState()
        raise streamer_mod._UnproductiveSession(
            "no bars", bars_received=0,
        )

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    with pytest.raises(streamer_mod.UnproductiveReconnectError):
        await s.run(MagicMock())

    # cap=3 ⇒ exactly 3 sessions before surrender
    assert session_count == 3


async def test_run_surrender_log_marker_after_cap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """reconnect_unproductive + reconnect_surrender markers fire before the error escalates."""
    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        return None

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=0,
        unproductive_reconnect_cap=2,
    )
    s.reconnect_delay = 0

    async def fake_run_session(consumer: Any) -> None:
        s._last_session_state = streamer_mod._SessionState()
        raise streamer_mod._UnproductiveSession(
            "no bars", bars_received=0,
        )

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    with caplog.at_level(logging.INFO, logger="tradedesk.execution.ig.price_streamer"):
        with pytest.raises(streamer_mod.UnproductiveReconnectError):
            await s.run(MagicMock())

    messages = [r.message for r in caplog.records]
    assert any("reconnect_unproductive" in m for m in messages)
    assert any(
        "reconnect_surrender" in m and "last_error=unproductive" in m
        for m in messages
    )


async def test_run_reauth_failure_counts_against_cap(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Re-auth failure increments the unproductive counter and surrenders at cap."""
    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        raise RuntimeError("network down")

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=0,
        unproductive_reconnect_cap=2,
    )
    s.reconnect_delay = 0

    async def fake_run_session(consumer: Any) -> None:
        s._last_session_state = streamer_mod._SessionState()
        raise streamer_mod.StaleStreamError("forced")

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    with caplog.at_level(logging.INFO, logger="tradedesk.execution.ig.price_streamer"):
        with pytest.raises(streamer_mod.UnproductiveReconnectError):
            await s.run(MagicMock())

    messages = [r.message for r in caplog.records]
    assert any(
        "reauth_result attempt=1 status=fail" in m for m in messages
    )
    assert any(
        "reconnect_surrender" in m and "last_error=reauth_failed" in m
        for m in messages
    )


async def test_run_unproductive_counter_resets_on_productive_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A productive session (bars > 0) ending in StaleStreamError zeros the unproductive counter.

    Without reset, three unproductive sessions interleaved with productive ones
    would still surrender at cap=2. With reset, the productive sessions zero the
    counter and the streamer runs through the whole pattern and exits cleanly.
    """
    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        return None

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=0,
        unproductive_reconnect_cap=2,
    )
    s.reconnect_delay = 0

    pattern: list[str] = [
        "unproductive", "stale", "unproductive", "stale", "unproductive",
    ]
    session_count = 0

    async def fake_run_session(consumer: Any) -> None:
        nonlocal session_count
        if session_count >= len(pattern):
            return  # Clean stream exit
        kind = pattern[session_count]
        session_count += 1
        state = streamer_mod._SessionState()
        if kind == "stale":
            state.bars_received = 5
            s._last_session_state = state
            raise streamer_mod.StaleStreamError("rolled over")
        s._last_session_state = state
        raise streamer_mod._UnproductiveSession(
            "no bars", bars_received=0,
        )

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    await s.run(MagicMock())  # Must exit cleanly — no surrender

    assert session_count == len(pattern)


# ──────────────────────────────────────────────────────────────────────────────
# run() — reconnect baseline reset (td#2) + recovery success signal (td#12)
# ──────────────────────────────────────────────────────────────────────────────


async def test_run_resets_stale_baseline_before_reconnect_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reconnect session starts with a refreshed staleness baseline (td#2).

    ``consumer.last_update`` only advances on dispatch, so after a disconnect
    it still holds the ancient pre-disconnect timestamp. Without the reset the
    reconnect session's heartbeat monitor would see a huge delta on its first
    tick and immediately re-raise StaleStreamError → reconnect spin. The
    initial session keeps its baseline (freshly set in the consumer __init__).
    """
    from datetime import datetime, timezone

    _stub_ls_client(monkeypatch)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        return None

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=2,
        unproductive_reconnect_cap=10,
    )
    s.reconnect_delay = 0

    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    consumer = MagicMock()
    consumer.last_update = ancient

    seen_last_update: list[datetime] = []

    async def fake_run_session(c: Any) -> None:
        seen_last_update.append(c.last_update)
        s._last_session_state = streamer_mod._SessionState()
        s._last_session_state.bars_received = 1
        raise streamer_mod.StaleStreamError("forced")

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    with pytest.raises(streamer_mod.StaleStreamError):
        await s.run(consumer)

    # Initial session keeps the ancient baseline; the reconnect session sees a
    # freshly-reset (recent) baseline so it does not instantly re-stale.
    assert seen_last_update[0] == ancient
    assert seen_last_update[1] > ancient
    assert (
        datetime.now(timezone.utc) - seen_last_update[1]
    ).total_seconds() < 5


async def test_run_reconnect_recovery_emits_success_signal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Acceptance (td#9): stale → reconnect → productive session emits recovery.

    Drives run() through StaleStreamError → reconnect → a second (productive)
    session and asserts a single clean reconnect with refreshed tokens, a reset
    staleness baseline, and exactly one reconnect-recovery success signal so ops
    can distinguish reconnects merely *attempted* from those that *recovered*.
    """
    from datetime import datetime, timezone

    _stub_ls_client(monkeypatch)

    recoveries = MagicMock()
    monkeypatch.setattr(streamer_mod, "STREAM_RECONNECT_RECOVERIES", recoveries)

    ig_client = MagicMock()
    ig_client.ls_cst = "OLD_CST"

    reauth_calls: list[bool] = []

    async def fake_authenticate() -> None:
        reauth_calls.append(True)
        ig_client.ls_cst = "NEW_CST"

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=0,
        unproductive_reconnect_cap=10,
    )
    s.reconnect_delay = 0

    ancient = datetime(2020, 1, 1, tzinfo=timezone.utc)
    consumer = MagicMock()
    consumer.last_update = ancient

    sessions: list[tuple[str, Any]] = []
    call = {"n": 0}

    async def fake_run_session(c: Any) -> None:
        call["n"] += 1
        sessions.append((ig_client.ls_cst, c.last_update))
        if call["n"] <= 2:
            # Session 1 (initial) and session 2 (reconnect) are both
            # productive, then go stale → drive the reconnect path.
            s._last_session_state = streamer_mod._SessionState()
            s._last_session_state.bars_received = 7
            raise streamer_mod.StaleStreamError("rollover")
        return  # Third session exits cleanly to terminate run().

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    with caplog.at_level(
        logging.INFO, logger="tradedesk.execution.ig.price_streamer"
    ):
        await s.run(consumer)

    # Initial session uses the original tokens + ancient baseline; the
    # reconnect session sees refreshed tokens and a reset baseline.
    assert sessions[0][0] == "OLD_CST"
    assert sessions[0][1] == ancient
    assert sessions[1][0] == "NEW_CST"
    assert sessions[1][1] > ancient

    # Exactly one recovery: only session 2 was a productive *reconnect*.
    recoveries.inc.assert_called_once()
    assert any(
        "reconnect_recovered" in r.message for r in caplog.records
    )


async def test_run_initial_session_stale_not_counted_as_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A productive INITIAL session going stale is rollover, not a recovery (td#12)."""
    _stub_ls_client(monkeypatch)

    recoveries = MagicMock()
    monkeypatch.setattr(streamer_mod, "STREAM_RECONNECT_RECOVERIES", recoveries)

    ig_client = MagicMock()
    ig_client.ls_cst = "CST"

    async def fake_authenticate() -> None:
        return None

    ig_client.auth.authenticate = fake_authenticate

    s = streamer_mod.Lightstreamer(
        ig_client,
        max_reconnect_attempts=0,
        unproductive_reconnect_cap=10,
    )
    s.reconnect_delay = 0

    call = {"n": 0}

    async def fake_run_session(c: Any) -> None:
        call["n"] += 1
        if call["n"] == 1:
            s._last_session_state = streamer_mod._SessionState()
            s._last_session_state.bars_received = 5
            raise streamer_mod.StaleStreamError("rollover")
        return  # Reconnect session exits cleanly.

    monkeypatch.setattr(s, "_run_session", fake_run_session)

    await s.run(MagicMock())

    recoveries.inc.assert_not_called()
