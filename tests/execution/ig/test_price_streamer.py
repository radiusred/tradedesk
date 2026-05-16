"""Unit tests for tradedesk.execution.ig.price_streamer

Covers:
  - RetryScheduler: scheduling, cancellation, and closed-state semantics
  - RetryScheduler._run: metric increment on action invocation
  - _MarketListener.onSubscriptionError: linear-backoff delay + max-retries cutoff
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
# _MarketListener — subscription retry path
# ──────────────────────────────────────────────────────────────────────────────


def test_market_subscription_error_schedules_with_linear_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """onSubscriptionError schedules retries at attempt * STREAM_SUB_RETRY_BASE_DELAY_S."""
    schedule_calls: list[tuple[float, Any, str]] = []

    def fake_schedule(
        self: Any, delay: float, action: Any, *, kind: str = "unknown"
    ) -> None:
        schedule_calls.append((delay, action, kind))

    monkeypatch.setattr(RetryScheduler, "schedule", fake_schedule)

    sched = RetryScheduler.__new__(RetryScheduler)
    listener = _make_market_listener(scheduler=sched)
    base = streamer_mod.STREAM_SUB_RETRY_BASE_DELAY_S

    listener.onSubscriptionError(503, "unavailable")
    assert len(schedule_calls) == 1
    assert schedule_calls[0][0] == pytest.approx(1 * base)
    assert schedule_calls[0][2] == "market"

    listener.onSubscriptionError(503, "unavailable")
    assert len(schedule_calls) == 2
    assert schedule_calls[1][0] == pytest.approx(2 * base)


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
    bad_update.getValue.side_effect = RuntimeError("parse failure")

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
