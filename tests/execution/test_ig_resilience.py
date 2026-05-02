"""Resilience tests for IG execution layer (RAD-747).

Covers the four acceptance criteria:
  - asyncio-based subscription-retry scheduler cancels cleanly on disconnect
  - concurrent OAuth refresh callers share a single in-flight request
  - SIGTERM triggers ordered shutdown of the runner
  - reconnect/auth-refresh metrics are emitted to Prometheus
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from prometheus_client import REGISTRY

import tradedesk.execution.ig.price_streamer as ig_streamer
import tradedesk.runner as runner_mod
from tradedesk.execution.ig.auth import IGAuthManager, TokenState
from tradedesk.execution.ig.price_streamer import RetryScheduler

# ---------------------------------------------------------------------------
# RetryScheduler — asyncio-based, cancellable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_scheduler_runs_action_after_delay() -> None:
    loop = asyncio.get_running_loop()
    scheduler = RetryScheduler(loop)
    fired = asyncio.Event()

    scheduler.schedule(0.01, fired.set, kind="market")
    await asyncio.wait_for(fired.wait(), timeout=1.0)


@pytest.mark.asyncio
async def test_retry_scheduler_cancel_all_prevents_action() -> None:
    """cancel_all stops pending retries before they fire."""
    loop = asyncio.get_running_loop()
    scheduler = RetryScheduler(loop)
    counter = {"calls": 0}

    def action() -> None:
        counter["calls"] += 1

    scheduler.schedule(5.0, action, kind="market")
    # Let the spawn callback execute on the loop.
    await asyncio.sleep(0.01)
    await scheduler.cancel_all()
    # Sleep past when the action would have run if not cancelled.
    await asyncio.sleep(0.05)

    assert counter["calls"] == 0


@pytest.mark.asyncio
async def test_retry_scheduler_no_schedule_after_close() -> None:
    """Schedule calls after cancel_all are silently dropped."""
    loop = asyncio.get_running_loop()
    scheduler = RetryScheduler(loop)
    await scheduler.cancel_all()

    counter = {"calls": 0}

    def action() -> None:
        counter["calls"] += 1

    scheduler.schedule(0.01, action, kind="market")
    await asyncio.sleep(0.05)

    assert counter["calls"] == 0


# ---------------------------------------------------------------------------
# Streamer.disconnect cancels pending retries
# ---------------------------------------------------------------------------


class _FakeSubscription:
    def __init__(self, mode: str, items: list[str], fields: list[str]) -> None:
        self.mode = mode
        self.items = items
        self.fields = fields
        self._listener: Any = None

    def addListener(self, listener: Any) -> None:
        self._listener = listener

    def setDataAdapter(self, adapter: str) -> None:
        pass


@pytest.fixture()
def patched_streamer(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    monkeypatch.setattr(ig_streamer, "Subscription", _FakeSubscription)
    ls_client = MagicMock()
    ls_client.connectionDetails = MagicMock()
    monkeypatch.setattr(
        ig_streamer, "LightstreamerClient", lambda *a, **k: ls_client
    )
    return ls_client


@pytest.mark.asyncio
async def test_disconnect_cancels_pending_subscription_retries(
    patched_streamer: MagicMock,
) -> None:
    """disconnect() cancels in-flight retries via the scheduler."""
    from tradedesk.marketdata.subscriptions import (
        ChartSubscription,
        MarketSubscription,
    )
    from tradedesk.strategy.base import BaseStrategy

    ig_client = MagicMock()
    ig_client.ls_url = "https://example"
    ig_client.ls_cst = "CST"
    ig_client.ls_xst = "XST"
    ig_client.client_id = "CID"
    ig_client.account_id = "AID"

    class _Strat(BaseStrategy):
        SUBSCRIPTIONS = [
            MarketSubscription("CS.D.EURUSD.CFD.IP", account_id="AID"),
            ChartSubscription("CS.D.EURUSD.CFD.IP", "5MINUTE"),
        ]

        async def on_price_update(self, market_data: Any) -> None:
            return None

    strat = _Strat(ig_client)
    strat._handle_event = AsyncMock()  # type: ignore[attr-defined]

    streamer = ig_streamer.Lightstreamer(ig_client)
    task = asyncio.create_task(streamer.run(strat))
    await asyncio.sleep(0.05)

    market_sub = next(
        s for s in patched_streamer.subscribe.call_args_list
        if s.args[0].items[0].startswith("PRICE:")
    ).args[0]

    # Use a long retry delay so the retry stays pending until we cancel.
    market_sub._listener.onSubscriptionError(21, "transient")
    await asyncio.sleep(0.01)
    assert streamer._scheduler is not None
    assert len(streamer._scheduler._tasks) == 1

    task.cancel()
    await task

    # After session teardown, the scheduler is closed and tasks are gone.
    assert streamer._scheduler is None


# ---------------------------------------------------------------------------
# Single-flight OAuth refresh
# ---------------------------------------------------------------------------


def _make_settings() -> Any:
    from tradedesk.execution.ig.settings import Settings

    with patch.dict(
        os.environ,
        {
            "IG_API_KEY": "key",
            "IG_USERNAME": "user",
            "IG_PASSWORD": "pass",
            "IG_ENVIRONMENT": "DEMO",
        },
    ):
        return Settings()


def _make_oauth_client() -> MagicMock:
    client = MagicMock()
    client.base_url = "https://demo-api.ig.com/gateway/deal"
    client.api_version = "3"
    client.headers = {"VERSION": "3", "X-IG-API-KEY": "key"}
    client._session = None
    client._apply_session_headers = MagicMock()
    return client


@pytest.mark.asyncio
async def test_concurrent_refreshes_share_single_request() -> None:
    """Concurrent ensure_valid() callers issue exactly one auth request."""
    auth = IGAuthManager(_make_oauth_client(), _make_settings())
    auth.uses_oauth = True
    auth.oauth_expires_at = time.time() - 1  # already expired
    auth.min_auth_interval = 0  # don't sleep

    auth_calls = {"count": 0}

    async def fake_request() -> tuple[dict[str, Any], dict[str, Any]]:
        auth_calls["count"] += 1
        await asyncio.sleep(0.05)
        return (
            {},
            {
                "oauthToken": {
                    "access_token": "tok",
                    "refresh_token": "ref",
                    "expires_in": "60",
                },
                "accountId": "A1",
                "clientId": "C1",
            },
        )

    auth._perform_auth_request = fake_request  # type: ignore[assignment]

    await asyncio.gather(*(auth.ensure_valid() for _ in range(5)))

    assert auth_calls["count"] == 1
    assert auth.token_state == TokenState.REFRESHED


@pytest.mark.asyncio
async def test_ensure_valid_skips_when_token_still_valid() -> None:
    """ensure_valid() is a no-op when the token has not expired."""
    auth = IGAuthManager(_make_oauth_client(), _make_settings())
    auth.uses_oauth = True
    auth.oauth_expires_at = time.time() + 60

    auth._perform_auth_request = AsyncMock()  # type: ignore[assignment]

    await auth.ensure_valid()

    auth._perform_auth_request.assert_not_called()


@pytest.mark.asyncio
async def test_token_state_marked_expired_on_failure() -> None:
    """A failed authenticate() leaves the token marked EXPIRED."""
    auth = IGAuthManager(_make_oauth_client(), _make_settings())
    auth.uses_oauth = True
    auth.oauth_expires_at = time.time() - 1
    auth.min_auth_interval = 0

    async def failing_request() -> tuple[dict[str, Any], dict[str, Any]]:
        raise RuntimeError("network error")

    auth._perform_auth_request = failing_request  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="network error"):
        await auth.ensure_valid()

    assert auth.token_state == TokenState.EXPIRED


# ---------------------------------------------------------------------------
# SIGTERM ordered shutdown
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self) -> None:
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


class _StubPortfolio:
    def __init__(self) -> None:
        self.run_started = asyncio.Event()
        self.cancelled = False

    async def run(self) -> None:
        self.run_started.set()
        try:
            await asyncio.Event().wait()  # block forever
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.mark.skipif(
    not hasattr(signal, "SIGTERM"),
    reason="SIGTERM not available on this platform",
)
@pytest.mark.asyncio
async def test_sigterm_triggers_ordered_shutdown() -> None:
    """SIGTERM cancels the portfolio task and closes the client."""
    client = _StubClient()
    portfolio = _StubPortfolio()

    async def driver() -> None:
        run_task = asyncio.create_task(
            runner_mod._async_run_portfolio(
                portfolio_factory=lambda c: portfolio,  # type: ignore[arg-type]
                client_factory=lambda: client,  # type: ignore[arg-type]
                log_level=None,
                setup_logging=False,
            )
        )
        await portfolio.run_started.wait()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(run_task, timeout=2.0)

    await driver()

    assert portfolio.cancelled is True
    assert client.closed is True


# ---------------------------------------------------------------------------
# Prometheus metrics are exposed
# ---------------------------------------------------------------------------


def test_resilience_metrics_registered() -> None:
    """Reconnect/auth-refresh metrics live in the default Prometheus registry."""
    expected = {
        "tradedesk_ig_stream_reconnects_total",
        "tradedesk_ig_stream_stale_seconds",
        "tradedesk_ig_subscription_retries_total",
        "tradedesk_ig_auth_refreshes_total",
        "tradedesk_ig_auth_refresh_inflight",
    }
    found = {
        m.name for m in REGISTRY.collect()
        if m.name in expected
        or any(s.name.startswith(m.name) for s in m.samples)
    }
    # Metric.name strips the suffix for histograms — relax to substring match.
    metric_names = {m.name for m in REGISTRY.collect()}
    for name in expected:
        # Counters are exposed without the _total suffix on Metric.name.
        base = name.removesuffix("_total")
        assert (
            name in metric_names or base in metric_names or name in found
        ), f"metric {name} not registered"
