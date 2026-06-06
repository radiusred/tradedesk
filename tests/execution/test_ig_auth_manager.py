"""Unit tests for IGAuthManager."""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from tradedesk.execution.ig.auth import IGAuthManager, _redact
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

    async def test_updates_last_auth_attempt(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())
        auth.last_auth_attempt = 0

        before = time.time()
        await auth._enforce_rate_limit()

        assert auth.last_auth_attempt >= before


class TestAuthenticate:
    async def test_dispatches_v2_auth(self) -> None:
        client = _make_client("2")
        auth = IGAuthManager(client, _make_settings())
        auth.last_auth_attempt = 0

        auth._perform_auth_request = AsyncMock(
            return_value=(
                {"CST": "c", "X-SECURITY-TOKEN": "x"},
                {"currentAccountId": "A1", "clientId": "C1"},
            )
        )

        await auth.authenticate()

        assert auth.ls_cst == "c"
        assert auth.ls_xst == "x"
        assert auth.account_id == "A1"
        assert auth.uses_oauth is False

    async def test_dispatches_v3_auth(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())
        auth.last_auth_attempt = 0

        auth._perform_auth_request = AsyncMock(
            return_value=(
                {},
                {
                    "oauthToken": {
                        "access_token": "tok",
                        "refresh_token": "ref",
                        "expires_in": "120",
                    },
                    "accountId": "A1",
                    "clientId": "C1",
                },
            )
        )

        await auth.authenticate()

        assert auth.oauth_access_token == "tok"
        assert auth.uses_oauth is True
        assert auth.account_id == "A1"

    async def test_rate_limits_before_auth(self) -> None:
        client = _make_client("2")
        auth = IGAuthManager(client, _make_settings())
        auth.last_auth_attempt = time.time()
        auth.min_auth_interval = 0.05

        auth._perform_auth_request = AsyncMock(
            return_value=(
                {"CST": "c", "X-SECURITY-TOKEN": "x"},
                {"currentAccountId": "A1"},
            )
        )

        start = time.monotonic()
        await auth.authenticate()
        elapsed = time.monotonic() - start

        assert elapsed >= 0.04


class TestPerformAuthRequest:
    async def test_raises_on_network_error(self) -> None:
        client = _make_client()
        settings = _make_settings()
        auth = IGAuthManager(client, settings)

        mock_session = MagicMock()
        mock_session.post = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        client._session = mock_session

        with pytest.raises(RuntimeError, match="Network error"):
            await auth._perform_auth_request()

    async def test_delegates_non_200_to_error_handler(self) -> None:
        client = _make_client()
        settings = _make_settings()
        auth = IGAuthManager(client, settings)

        mock_resp = AsyncMock()
        mock_resp.status = 401
        mock_resp.json = AsyncMock(return_value={"errorCode": "invalid-creds"})
        mock_resp.headers = {"Content-Type": "application/json"}

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=ctx)
        client._session = mock_session

        with pytest.raises(RuntimeError, match="HTTP 401"):
            await auth._perform_auth_request()

    async def test_creates_session_if_none(self) -> None:
        client = _make_client()
        settings = _make_settings()
        auth = IGAuthManager(client, settings)
        client._session = None

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(return_value={"currentAccountId": "A1"})
        mock_resp.headers = {"CST": "c", "X-SECURITY-TOKEN": "x"}

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_session = MagicMock()
            mock_session.post = MagicMock(return_value=ctx)
            mock_cls.return_value = mock_session

            headers, body = await auth._perform_auth_request()

            mock_cls.assert_called_once_with(headers=client.headers)
            assert body == {"currentAccountId": "A1"}

    async def test_handles_json_parse_failure(self) -> None:
        client = _make_client()
        settings = _make_settings()
        auth = IGAuthManager(client, settings)

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json = AsyncMock(side_effect=ValueError("bad json"))
        mock_resp.headers = {"CST": "c"}

        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_resp)
        ctx.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=ctx)
        client._session = mock_session

        headers, body = await auth._perform_auth_request()

        assert body == {}


class TestHandleAuthErrorExtended:
    async def test_falls_back_to_text_body(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        resp = MagicMock()
        resp.status = 500
        resp.json = AsyncMock(side_effect=ValueError("not json"))
        resp.text = AsyncMock(return_value="Internal Server Error")

        with pytest.raises(RuntimeError, match="HTTP 500"):
            await auth._handle_auth_error(resp)

    async def test_403_non_rate_limit_error(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        resp = MagicMock()
        resp.status = 403
        resp.json = AsyncMock(return_value={"errorCode": "error.other-forbidden"})

        with pytest.raises(RuntimeError, match="HTTP 403"):
            await auth._handle_auth_error(resp)


class TestHandleV2AuthExtended:
    def test_falls_back_to_account_id_key(self) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        headers = {"CST": "c", "X-SECURITY-TOKEN": "x"}
        body = {"accountId": "FALLBACK_ACC"}

        auth._handle_v2_auth(headers, body)
        assert auth.account_id == "FALLBACK_ACC"


class TestStoreOauthToken:
    async def test_calculates_expires_at_with_buffer(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        before = time.time()
        await auth._store_oauth_token(
            {"access_token": "tok", "expires_in": "60"},
            "A1",
            "C1",
        )

        assert auth.oauth_expires_at >= before + 55 - 1
        assert auth.oauth_expires_at <= before + 55 + 1

    async def test_applies_bearer_header(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        await auth._store_oauth_token(
            {"access_token": "my_token", "expires_in": "30"},
            "A1",
            "C1",
        )

        client._apply_session_headers.assert_called_once_with(
            {"Authorization": "Bearer my_token", "IG-ACCOUNT-ID": "A1"}
        )

    async def test_defaults_expires_in_to_30(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        before = time.time()
        await auth._store_oauth_token(
            {"access_token": "tok"},
            "A1",
            "C1",
        )

        assert auth.oauth_expires_at >= before + 25 - 1
        assert auth.oauth_expires_at <= before + 25 + 1

    async def test_stores_refresh_token(self) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        await auth._store_oauth_token(
            {"access_token": "a", "refresh_token": "r", "expires_in": "60"},
            "A1",
            "C1",
        )

        assert auth.oauth_refresh_token == "r"
        assert auth.client_id == "C1"


# ---------------------------------------------------------------------------
# _redact helper tests
# ---------------------------------------------------------------------------


class TestRedact:
    def test_redacts_cst_header(self) -> None:
        result = _redact({"CST": "super-secret-token", "Other": "visible"})
        assert result["CST"].startswith("<redacted:")
        assert "super-secret-token" not in result["CST"]
        assert result["Other"] == "visible"

    def test_redacts_x_security_token_header(self) -> None:
        result = _redact({"X-SECURITY-TOKEN": "abc123xyz"})
        assert result["X-SECURITY-TOKEN"] == "<redacted:9>"

    def test_redacts_authorization_header(self) -> None:
        result = _redact({"Authorization": "Bearer my-oauth-token"})
        assert result["Authorization"].startswith("<redacted:")
        assert "my-oauth-token" not in result["Authorization"]

    def test_redacts_access_token_in_body(self) -> None:
        result = _redact({"access_token": "tok123", "accountId": "A1"})
        assert result["access_token"].startswith("<redacted:")
        assert result["accountId"] == "A1"

    def test_redacts_refresh_token_in_body(self) -> None:
        result = _redact({"refresh_token": "ref456"})
        assert result["refresh_token"].startswith("<redacted:")

    def test_redacts_nested_access_token(self) -> None:
        result = _redact({
            "oauthToken": {"access_token": "nested-tok", "expires_in": "60"},
            "accountId": "A1",
        })
        assert result["oauthToken"]["access_token"].startswith("<redacted:")
        assert result["oauthToken"]["expires_in"] == "60"
        assert result["accountId"] == "A1"

    def test_passthrough_non_sensitive_dict(self) -> None:
        data = {"status": "ok", "accountId": "A1", "clientId": "C1"}
        assert _redact(data) == data

    def test_passthrough_string_body(self) -> None:
        assert _redact("plain error text") == "plain error text"

    def test_passthrough_none(self) -> None:
        assert _redact(None) is None

    def test_redact_length_reflects_value(self) -> None:
        result = _redact({"CST": "12345"})
        assert result["CST"] == "<redacted:5>"

    def test_redacts_lowercase_authorization_header(self) -> None:
        result = _redact({"authorization": "Bearer lower-token"})
        assert result["authorization"].startswith("<redacted:")
        assert "lower-token" not in result["authorization"]

    def test_redacts_title_cased_headers(self) -> None:
        # IG / proxies may return headers in title or mixed case; matching
        # must be case-insensitive so none of these casings leak.
        result = _redact({
            "Cst": "secret-cst",
            "X-Security-Token": "secret-xsec",
            "Authorization": "Bearer secret-bearer",
        })
        assert result["Cst"].startswith("<redacted:")
        assert result["X-Security-Token"].startswith("<redacted:")
        assert result["Authorization"].startswith("<redacted:")
        assert "secret-cst" not in str(result)
        assert "secret-xsec" not in str(result)
        assert "secret-bearer" not in str(result)

    def test_redacts_token_in_list(self) -> None:
        result = _redact({"outer": [{"access_token": "tok123"}, {"other": "val"}]})
        assert result["outer"][0]["access_token"].startswith("<redacted:")
        assert "tok123" not in result["outer"][0]["access_token"]
        assert result["outer"][1]["other"] == "val"

    def test_redacts_deeply_nested_token(self) -> None:
        deep: dict = {"access_token": "deep-secret"}
        for _ in range(10):
            deep = {"level": deep}
        result = _redact(deep)
        inner = result
        for _ in range(10):
            inner = inner["level"]
        assert inner["access_token"].startswith("<redacted:")
        assert "deep-secret" not in inner["access_token"]


# ---------------------------------------------------------------------------
# Token-redaction integration: log messages must not leak secrets
# ---------------------------------------------------------------------------


class TestAuthErrorLogsRedacted:
    async def test_handle_auth_error_does_not_log_cst_in_body(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        resp = MagicMock()
        resp.status = 401
        resp.json = AsyncMock(return_value={"cst": "SECRET_CST", "errorCode": "bad"})

        import logging

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await auth._handle_auth_error(resp)

        assert "SECRET_CST" not in caplog.text

    def test_handle_v2_auth_does_not_log_cst_on_missing_tokens(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _make_client()
        auth = IGAuthManager(client, _make_settings())

        import logging

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                auth._handle_v2_auth({"CST": "HEADER_SECRET"}, {"cst": "BODY_SECRET"})

        assert "HEADER_SECRET" not in caplog.text
        assert "BODY_SECRET" not in caplog.text

    async def test_handle_v3_auth_does_not_log_access_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = _make_client("3")
        auth = IGAuthManager(client, _make_settings())

        import logging

        with caplog.at_level(logging.ERROR):
            with pytest.raises(RuntimeError):
                await auth._handle_v3_auth({
                    "oauthToken": {"refresh_token": "REFRESH_SECRET"},
                    "accountId": "A1",
                })

        assert "REFRESH_SECRET" not in caplog.text
