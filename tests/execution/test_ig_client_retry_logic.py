from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.execution.ig.client import IGClient


class FakeResp:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


def _make_session(resp: FakeResp) -> MagicMock:
    """Return a minimal mock ClientSession whose request() is a CM yielding resp."""
    session = MagicMock()
    session.headers = {}
    session.request = MagicMock(return_value=resp)
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
    """quantise_size must not crash when the market snapshot has dealingRules: null.

    IG DEMO can return {"dealingRules": null, ...}. dict.get("key", {}) returns None
    (not {}) when the key is present but has a null value, causing:
        AttributeError: 'NoneType' object has no attribute 'get'
    """
    c = IGClient()
    # Snapshot with explicit null dealingRules (as IG DEMO sometimes returns)
    c.get_instrument_metadata = AsyncMock(
        return_value={"dealingRules": None, "instrument": {}}
    )

    result = await c.quantise_size("CS.D.GBPUSD.TODAY.IP", 1.0)
    assert result == 1.0


@pytest.mark.asyncio
async def test_quantise_size_null_min_deal_size():
    """quantise_size must not crash when minDealSize is present but null."""
    c = IGClient()
    c.get_instrument_metadata = AsyncMock(
        return_value={"dealingRules": {"minDealSize": None}}
    )

    result = await c.quantise_size("CS.D.GBPUSD.TODAY.IP", 0.5)
    assert result == 0.5
