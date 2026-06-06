from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.execution.ig.client import IGClient


class FakeResp:
    def __init__(self, status: int, body, *, text: str | None = None):
        self.status = status
        self._body = body
        self._text = text

    async def json(self):
        if self._body is _NO_JSON:
            raise ValueError("no json body")
        return self._body

    async def text(self):
        return self._text if self._text is not None else ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# Sentinel marking a response whose json() should raise (non-JSON body).
_NO_JSON = object()


def _make_session(resp: FakeResp) -> MagicMock:
    """Return a minimal mock ClientSession whose request() is a CM yielding resp."""
    session = MagicMock()
    session.headers = {}
    session.request = MagicMock(return_value=resp)
    return session


def _make_sequence_session(*resps: FakeResp) -> MagicMock:
    """Mock ClientSession whose request() yields each resp in turn (one per call)."""
    session = MagicMock()
    session.headers = {}
    session.request = MagicMock(side_effect=list(resps))
    return session


@pytest.mark.asyncio
async def test_handle_retry_logic_raises_on_rate_limit():
    c = IGClient()
    c._authenticate = AsyncMock()  # type: ignore[attr-defined]

    resp = FakeResp(
        status=403,
        body={"errorCode": "error.public-api.exceeded-api-key-allowance"},
    )

    with pytest.raises(RuntimeError, match="rate limit"):
        await c._handle_retry_logic(resp, "GET", "url")  # type: ignore[arg-type]

    c._authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retry_logic_reauths_on_401_403_non_rate_limit():
    c = IGClient()
    c._authenticate = AsyncMock()  # type: ignore[attr-defined]

    resp = FakeResp(status=401, body={"errorCode": "some.other.error"})
    await c._handle_retry_logic(resp, "GET", "url")  # type: ignore[arg-type]

    c._authenticate.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_deal_retries_404_deal_not_found_then_succeeds():
    c = IGClient()

    c._request = AsyncMock(
        side_effect=[
            RuntimeError(
                "IG request failed: HTTP 404: {'errorCode': 'error.confirms.deal-not-found'}"
            ),
            {"dealStatus": "ACCEPTED"},
        ]
    )

    res = await c.confirm_deal("ABC", timeout_s=1.0, poll_s=0.0)
    assert res["dealStatus"] == "ACCEPTED"
    assert c._request.await_count == 2


# ---------------------------------------------------------------------------
# _request: null / non-dict JSON body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_returns_empty_dict_when_json_body_is_null():
    """_request must return {} when resp.json() is None (e.g. IG returns null body).

    Before the fix _request returned None, which propagated into confirm_deal
    and caused: AttributeError: 'NoneType' object has no attribute 'get'
    """
    c = IGClient()
    c._session = _make_session(FakeResp(status=200, body=None))

    result = await c._request("GET", "/confirms/FAKE")

    assert result == {}
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_request_returns_empty_dict_when_json_body_is_non_dict():
    """_request must return {} for any non-dict JSON (list, scalar, etc.)."""
    c = IGClient()
    c._session = _make_session(FakeResp(status=200, body=["unexpected", "list"]))

    result = await c._request("GET", "/some/path")

    assert result == {}
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_confirm_deal_null_body_raises_timeout_not_attribute_error():
    """confirm_deal with a null confirm response must raise TimeoutError, not AttributeError.

    Before the fix: _request returned None → payload.get() → AttributeError.
    After the fix: _request returns {} → dealStatus never set → TimeoutError.
    """
    c = IGClient()
    # Simulate the fixed _request always returning {} (null body scenario)
    c._request = AsyncMock(return_value={})

    with pytest.raises(TimeoutError):
        await c.confirm_deal("REF", timeout_s=0.01, poll_s=0.0)


@pytest.mark.asyncio
async def test_quantise_size_null_dealing_rules():
    """quantise_size falls back to 2 dp when dealingRules is null.

    IG DEMO can return {"dealingRules": null, ...}.  Rather than passing a raw
    float with 15 decimal places to the broker (which returns HTTP 400
    "validation.number.too-many-decimal-places.request.size"), we round to 2 dp.
    """
    c = IGClient()
    # Snapshot with explicit null dealingRules (as IG DEMO sometimes returns)
    c.get_instrument_metadata = AsyncMock(return_value={"dealingRules": None, "instrument": {}})

    result = await c.quantise_size("CS.D.GBPUSD.TODAY.IP", 1.0)
    assert result == 1.0


@pytest.mark.asyncio
async def test_quantise_size_null_min_deal_size():
    """quantise_size falls back to 2 dp when minDealSize is present but null."""
    c = IGClient()
    c.get_instrument_metadata = AsyncMock(return_value={"dealingRules": {"minDealSize": None}})

    result = await c.quantise_size("CS.D.GBPUSD.TODAY.IP", 0.5)
    assert result == 0.5


@pytest.mark.asyncio
async def test_quantise_size_null_dealing_rules_truncates_long_float():
    """When dealingRules is null, a raw float with many decimal places is rounded to 2 dp.

    Regression test: commit 30bc110 changed the null-check from dict.get(key, {}) to
    `or {}`, which prevented an AttributeError but silently returned the unquantised
    float (e.g. 9.544888884913714), causing IG to reject it with HTTP 400
    "validation.number.too-many-decimal-places.request.size".
    """
    c = IGClient()
    c.get_instrument_metadata = AsyncMock(return_value={"dealingRules": None, "instrument": {}})

    result = await c.quantise_size("CS.D.USDJPY.TODAY.IP", 9.544888884913714)
    assert result == 9.54


# ---------------------------------------------------------------------------
# _request: bounded re-auth retry (RAD-3729)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_reissues_with_refreshed_token_after_401():
    """A 401 triggers one re-auth and the request is re-issued; the 200 wins.

    Regression: before the fix _request raised on the consumed 401 response even
    after _handle_retry_logic re-authenticated — the refreshed token was never
    used.
    """
    c = IGClient()
    c._authenticate = AsyncMock()  # type: ignore[attr-defined]
    c._session = _make_sequence_session(
        FakeResp(status=401, body={"errorCode": "error.security.client-token-invalid"}),
        FakeResp(status=200, body={"ok": True}),
    )

    result = await c._request("GET", "/positions")

    assert result == {"ok": True}
    c._authenticate.assert_awaited_once()
    assert c._session.request.call_count == 2


@pytest.mark.asyncio
async def test_request_reauth_retry_is_bounded_to_single_attempt():
    """A persistently-401 endpoint re-auths exactly once, then surfaces the error.

    Guards against an infinite re-auth/retry loop when the credential is
    genuinely rejected.
    """
    c = IGClient()
    c._authenticate = AsyncMock()  # type: ignore[attr-defined]
    c._session = _make_sequence_session(
        FakeResp(status=401, body={"errorCode": "error.security.client-token-invalid"}),
        FakeResp(status=401, body={"errorCode": "error.security.client-token-invalid"}),
    )

    with pytest.raises(RuntimeError, match="HTTP 401"):
        await c._request("GET", "/positions")

    c._authenticate.assert_awaited_once()  # bounded: not called twice
    assert c._session.request.call_count == 2


@pytest.mark.asyncio
async def test_request_surfaces_errorcode_on_non_auth_failure():
    """A 400 with a JSON errorCode surfaces that code in the raised message."""
    c = IGClient()
    c._session = _make_session(
        FakeResp(
            status=400,
            body={"errorCode": "validation.null-not-allowed.request.epic"},
        )
    )

    with pytest.raises(RuntimeError) as excinfo:
        await c._request("POST", "/positions/otc")

    msg = str(excinfo.value)
    assert "HTTP 400" in msg
    assert "errorCode=validation.null-not-allowed.request.epic" in msg


@pytest.mark.asyncio
async def test_request_non_json_error_body_is_stripped_and_truncated():
    """A non-JSON/HTML error body is still stripped, with no spurious errorCode."""
    c = IGClient()
    html = "<html><body><h1>Gateway Timeout</h1></body></html>"
    c._session = _make_session(FakeResp(status=504, body=_NO_JSON, text=html))

    with pytest.raises(RuntimeError) as excinfo:
        await c._request("GET", "/positions")

    msg = str(excinfo.value)
    assert "HTTP 504" in msg
    assert "Gateway Timeout" in msg
    assert "errorCode=" not in msg  # no JSON errorCode to surface
