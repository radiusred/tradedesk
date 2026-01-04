# tests/test_client.py
"""
Tests for the IGClient class.
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tradedesk.providers.ig.client import IGClient


class TestIGClient:
    @pytest.mark.asyncio
    async def test_initialization_demo(self):
        client = IGClient()
        assert client.base_url == "https://demo-api.ig.com/gateway/deal"
        assert client.ls_url == "https://demo-apd.marketdatasystems.com"
        assert client.headers["VERSION"] == "2"

    @pytest.mark.asyncio
    async def test_initialization_live(self):
        with patch("tradedesk.providers.ig.client.settings") as mock_settings:
            mock_settings.ig_environment = "LIVE"
            mock_settings.ig_api_key = "test-key"
            mock_settings.ig_username = "test-user"
            mock_settings.ig_password = "test-pass"

            client = IGClient()
            assert client.base_url == "https://api.ig.com/gateway/deal"
            assert client.ls_url == "https://apd.marketdatasystems.com"
            assert client.headers["VERSION"] == "2"

    @pytest.mark.asyncio
    async def test_place_market_order_defaults_include_expiry_tif_gbp_netting(self, mock_aiohttp_session):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"dealReference": "REF123"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            res = await client.place_market_order(epic="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
            assert res == {"dealReference": "REF123"}

            (_, url), kwargs = mock_aiohttp_session.request.call_args
            assert url.endswith("/positions/otc")
            body = kwargs["json"]
            assert body["currencyCode"] == "GBP"
            assert body["forceOpen"] is False
            assert body["expiry"] == "-"
            assert body["timeInForce"] == "FILL_OR_KILL"
            assert body["guaranteedStop"] is False

    @pytest.mark.asyncio
    async def test_confirm_deal_polls_until_not_pending(self, mock_aiohttp_session):
        pending = MagicMock()
        pending.status = 200
        pending.json = AsyncMock(return_value={"dealStatus": "PENDING"})
        pending.__aenter__ = AsyncMock(return_value=pending)
        pending.__aexit__ = AsyncMock(return_value=None)

        accepted = MagicMock()
        accepted.status = 200
        accepted.json = AsyncMock(return_value={"dealStatus": "ACCEPTED", "dealId": "D1"})
        accepted.__aenter__ = AsyncMock(return_value=accepted)
        accepted.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.side_effect = [pending, accepted]

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            payload = await client.confirm_deal("REF123", timeout_s=1.0, poll_s=0.0)
            assert payload["dealStatus"] == "ACCEPTED"
            assert payload["dealId"] == "D1"

    def test_token_validation(self):
        client = IGClient()
        client.uses_oauth = True

        client.oauth_expires_at = time.time() + 100
        assert client._is_token_valid() is True

        client.oauth_expires_at = time.time() - 100
        assert client._is_token_valid() is False

        client.uses_oauth = False
        assert client._is_token_valid() is True
        
    @pytest.mark.asyncio
    async def test_confirm_deal_retries_on_transient_500(self, mock_aiohttp_session):
        """
        If _request raises RuntimeError for HTTP 500 on confirms, confirm_deal should retry.
        """
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            # Simulate: first call raises 500, second returns PENDING, third ACCEPTED
            async def fake_request(method, path, **kwargs):
                fake_request.calls += 1
                if fake_request.calls == 1:
                    raise RuntimeError("IG request failed: HTTP 500: {'errorCode': None}")
                if fake_request.calls == 2:
                    return {"dealStatus": "PENDING"}
                return {"dealStatus": "ACCEPTED", "dealId": "D1"}

            fake_request.calls = 0

            with patch.object(client, "_request", side_effect=fake_request):
                payload = await client.confirm_deal("REF123", timeout_s=1.0, poll_s=0.0)
                assert payload["dealStatus"] == "ACCEPTED"
                assert payload["dealId"] == "D1"
