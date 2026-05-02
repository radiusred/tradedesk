"""Tests for tradedesk.settings — operational tunables and env overrides."""

from __future__ import annotations

import importlib

import pytest

import tradedesk.settings as settings_mod


def _reload() -> None:
    importlib.reload(settings_mod)


@pytest.fixture(autouse=True)
def _restore_defaults():
    """Reload after each test to restore defaults regardless of monkeypatching."""
    yield
    _reload()


# ---------------------------------------------------------------------------
# Default values match historic literals so behaviour is unchanged
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_stream_sub_max_retries(self) -> None:
        assert settings_mod.STREAM_SUB_MAX_RETRIES == 3

    def test_stream_sub_retry_base_delay(self) -> None:
        assert settings_mod.STREAM_SUB_RETRY_BASE_DELAY_S == 2.0

    def test_stream_heartbeat_sleep(self) -> None:
        assert settings_mod.STREAM_HEARTBEAT_SLEEP_S == 10

    def test_stream_max_stale_default(self) -> None:
        assert settings_mod.STREAM_MAX_STALE_DEFAULT_S == 300.0

    def test_stream_reconnect_delay_default(self) -> None:
        assert settings_mod.STREAM_RECONNECT_DELAY_DEFAULT_S == 5.0

    def test_stream_silence_suppress_threshold(self) -> None:
        assert settings_mod.STREAM_SILENCE_SUPPRESS_THRESHOLD_S == 300.0

    def test_stream_heartbeat_suppressed_sleep(self) -> None:
        assert settings_mod.STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S == 60

    def test_ig_auth_min_interval(self) -> None:
        assert settings_mod.IG_AUTH_MIN_INTERVAL_S == 5.0

    def test_ig_deal_confirm_timeout(self) -> None:
        assert settings_mod.IG_DEAL_CONFIRM_TIMEOUT_S == 10.0

    def test_ig_deal_confirm_poll(self) -> None:
        assert settings_mod.IG_DEAL_CONFIRM_POLL_S == 0.25

    def test_order_request_timeout(self) -> None:
        assert settings_mod.ORDER_REQUEST_TIMEOUT_S == 30.0


# ---------------------------------------------------------------------------
# Env-var overrides take effect on reload
# ---------------------------------------------------------------------------


class TestEnvOverrides:
    def test_int_override(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADEDESK_STREAM_SUB_MAX_RETRIES", "7")
        _reload()
        assert settings_mod.STREAM_SUB_MAX_RETRIES == 7

    def test_float_override(self, monkeypatch) -> None:
        monkeypatch.setenv("IG_DEAL_CONFIRM_POLL_S", "0.5")
        _reload()
        assert settings_mod.IG_DEAL_CONFIRM_POLL_S == 0.5

    def test_invalid_int_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADEDESK_STREAM_HEARTBEAT_SLEEP_S", "not-a-number")
        _reload()
        assert settings_mod.STREAM_HEARTBEAT_SLEEP_S == 10

    def test_invalid_float_falls_back_to_default(self, monkeypatch) -> None:
        monkeypatch.setenv("TRADEDESK_ORDER_REQUEST_TIMEOUT_S", "garbage")
        _reload()
        assert settings_mod.ORDER_REQUEST_TIMEOUT_S == 30.0

    def test_empty_env_uses_default(self, monkeypatch) -> None:
        monkeypatch.setenv("IG_AUTH_MIN_INTERVAL_S", "")
        _reload()
        assert settings_mod.IG_AUTH_MIN_INTERVAL_S == 5.0


# ---------------------------------------------------------------------------
# Downstream consumers pick up the constants (smoke test)
# ---------------------------------------------------------------------------


class TestConsumers:
    def test_orders_use_settings_defaults(self) -> None:
        """IGOrderHandler.confirm_deal default arg pulls from settings."""
        import inspect

        from tradedesk.execution.ig.orders import IGOrderHandler

        sig = inspect.signature(IGOrderHandler.confirm_deal)
        assert sig.parameters["timeout_s"].default == settings_mod.IG_DEAL_CONFIRM_TIMEOUT_S
        assert sig.parameters["poll_s"].default == settings_mod.IG_DEAL_CONFIRM_POLL_S

    def test_request_order_default_timeout(self) -> None:
        import inspect

        from tradedesk.execution.order_handler import request_order

        sig = inspect.signature(request_order)
        assert sig.parameters["timeout"].default == settings_mod.ORDER_REQUEST_TIMEOUT_S

    def test_streamer_default_max_stale(self) -> None:
        import inspect

        from tradedesk.execution.ig.price_streamer import Lightstreamer

        sig = inspect.signature(Lightstreamer.__init__)
        assert (
            sig.parameters["max_stale_seconds"].default
            == settings_mod.STREAM_MAX_STALE_DEFAULT_S
        )
        assert (
            sig.parameters["reconnect_delay"].default
            == settings_mod.STREAM_RECONNECT_DELAY_DEFAULT_S
        )
