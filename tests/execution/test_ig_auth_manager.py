"""Unit tests for IGAuthManager."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tradedesk.execution.ig.auth import IGAuthManager
from tradedesk.execution.ig.settings import Settings


def _make_client(api_version: str = "2") -> MagicMock:
    client = MagicMock()
    client.base_url = "https://demo-api.ig.com/gateway/deal"
    client.api_version = api_version
    client.headers = {"VERSION": api_version, "X-IG-API-KEY": "test-key"}
    client._session = None
    client._apply_session_headers = MagicMock()
    return client


def _make_settings(**kwargs: str) -> Settings:
    with (
        patch.dict(
            "os.environ",
            {
                "IG_API_KEY": kwargs.get("api_key", "key"),
                "IG_USERNAME": kwargs.get("username", "user"),
                "IG_PASSWORD": kwargs.get("password", "pass"),
                "IG_ENVIRONMENT": kwargs.get("environment", "DEMO"),
            },
        )
    ):
        return Settings()


class TestIsTokenValid:
    def test_non_oauth_always_valid(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())
        auth.uses_oauth = False
        assert auth.is_token_valid() is True

    def test_oauth_valid_within_expiry(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())
        auth.uses_oauth = True
        auth.oauth_expires_at = time.time() + 100
        assert auth.is_token_valid() is True

    def test_oauth_invalid_after_expiry(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())
        auth.uses_oauth = True
        auth.oauth_expires_at = time.time() - 1
        assert auth.is_token_valid() is False


class TestHandleV2Auth:
    def test_sets_tokens_and_account_id(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        headers = {"CST": "cst_token", "X-SECURITY-TOKEN": "xst_token"}
        body = {"currentAccountId": "ACC123", "clientId": "CLIENT1"}

        auth._handle_v2_auth(headers, body)

        assert auth.ls_cst == "cst_token"
        assert auth.ls_xst == "xst_token"
        assert auth.account_id == "ACC123"
        assert auth.client_id == "CLIENT1"
        assert auth.uses_oauth is False

    def test_falls_back_to_body_tokens(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        headers: dict[str, str] = {}
        body = {
            "cst": "body_cst",
            "x-security-token": "body_xst",
            "currentAccountId": "ACC456",
        }

        auth._handle_v2_auth(headers, body)
        assert auth.ls_cst == "body_cst"
        assert auth.ls_xst == "body_xst"

    def test_raises_if_tokens_missing(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        with pytest.raises(RuntimeError, match="CST and X-SECURITY-TOKEN"):
            auth._handle_v2_auth({}, {})

    def test_raises_if_account_id_missing(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        headers = {"CST": "c", "X-SECURITY-TOKEN": "x"}
        with pytest.raises(RuntimeError, match="account id"):
            auth._handle_v2_auth(headers, {})

    def test_applies_session_headers(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        auth._handle_v2_auth(
            {"CST": "c", "X-SECURITY-TOKEN": "x"},
            {"currentAccountId": "A1"},
        )
        client._apply_session_headers.assert_called_once_with(
            {"CST": "c", "X-SECURITY-TOKEN": "x", "IG-ACCOUNT-ID": "A1"}
        )


class TestHandleV3Auth:
    async def test_raises_without_access_token(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        with pytest.raises(RuntimeError, match="OAuth access_token"):
            await auth._handle_v3_auth({"oauthToken": {}})

    async def test_stores_oauth_token(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        body = {
            "oauthToken": {
                "access_token": "acc",
                "refresh_token": "ref",
                "expires_in": "60",
            },
            "accountId": "A1",
            "clientId": "C1",
        }
        await auth._handle_v3_auth(body)

        assert auth.oauth_access_token == "acc"
        assert auth.oauth_refresh_token == "ref"
        assert auth.account_id == "A1"
        assert auth.uses_oauth is True


class TestHandleAuthError:
    async def test_raises_rate_limit_error(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        resp = MagicMock()
        resp.status = 403
        resp.json = AsyncMock(
            return_value={"errorCode": "error.public-api.exceeded-api-key-allowance"}
        )

        with pytest.raises(RuntimeError, match="rate limit"):
            await auth._handle_auth_error(resp)

    async def test_raises_generic_auth_error(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        resp = MagicMock()
        resp.status = 401
        resp.json = AsyncMock(return_value={"errorCode": "error.other"})

        with pytest.raises(RuntimeError, match="HTTP 401"):
            await auth._handle_auth_error(resp)


class TestRateLimit:
    async def test_waits_if_too_soon(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())
        auth.last_auth_attempt = time.time()  # just authenticated
        auth.min_auth_interval = 0.05

        start = time.monotonic()
        await auth._enforce_rate_limit()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.04  # waited at least ~50ms

    async def test_no_wait_if_enough_time_passed(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())
        auth.last_auth_attempt = time.time() - 10  # 10s ago
        auth.min_auth_interval = 5.0

        start = time.monotonic()
        await auth._enforce_rate_limit()
        elapsed = time.monotonic() - start

        assert elapsed < 0.01  # effectively immediate
