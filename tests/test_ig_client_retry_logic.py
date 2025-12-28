import pytest
from unittest.mock import AsyncMock, MagicMock

from tradedesk.providers.ig.client import IGClient


class FakeResp:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def json(self):
        return self._body


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
