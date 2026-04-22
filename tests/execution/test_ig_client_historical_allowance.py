from unittest.mock import AsyncMock

import pytest

from tradedesk.execution.broker import HistoricalDataAllowanceError
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


@pytest.mark.asyncio
async def test_handle_retry_logic_raises_on_historical_data_allowance():
    c = IGClient()
    c._authenticate = AsyncMock()

    resp = FakeResp(
        status=403,
        body={"errorCode": "error.public-api.exceeded-account-historical-data-allowance"},
    )

    with pytest.raises(HistoricalDataAllowanceError, match="historical data allowance"):
        await c._handle_retry_logic(resp, "GET", "url")

    c._authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retry_logic_still_raises_on_rate_limit():
    c = IGClient()
    c._authenticate = AsyncMock()

    resp = FakeResp(
        status=403,
        body={"errorCode": "error.public-api.exceeded-api-key-allowance"},
    )

    with pytest.raises(RuntimeError, match="rate limit"):
        await c._handle_retry_logic(resp, "GET", "url")

    c._authenticate.assert_not_called()


@pytest.mark.asyncio
async def test_handle_retry_logic_reauths_on_other_403():
    c = IGClient()
    c._authenticate = AsyncMock()

    resp = FakeResp(status=403, body={"errorCode": "some.other.error"})
    await c._handle_retry_logic(resp, "GET", "url")

    c._authenticate.assert_awaited_once()
