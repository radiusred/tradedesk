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
    async def test_place_market_order_sets_dfb_expiry_on_spreadbet_account(self, mock_aiohttp_session):
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"dealReference": "REF123"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            # Force SPREADBET behaviour without calling /accounts
            client._ensure_account_type = AsyncMock(return_value="SPREADBET")  # type: ignore[method-assign]

            await client.place_market_order(epic="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)

            (_, url), kwargs = mock_aiohttp_session.request.call_args
            assert url.endswith("/positions/otc")

            body = kwargs["json"]
            assert body["expiry"] == "DFB"

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

    @pytest.mark.asyncio
    async def test_get_instrument_metadata_caches_results(self, mock_aiohttp_session):
        """Test that get_instrument_metadata caches results and doesn't refetch unnecessarily."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "dealingRules": {
                "minDealSize": {"unit": "POINTS", "value": 0.5},
                "minStepDistance": {"unit": "POINTS", "value": 0.1},
            },
            "instrument": {"epic": "CS.D.EURUSD.TODAY.IP"},
            "snapshot": {"bid": 1.1, "offer": 1.1001},
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            # First call should fetch from API
            metadata1 = await client.get_instrument_metadata("CS.D.EURUSD.TODAY.IP")
            assert metadata1["dealingRules"]["minStepDistance"]["value"] == 0.1
            assert mock_aiohttp_session.request.call_count == 1

            # Second call should use cache
            metadata2 = await client.get_instrument_metadata("CS.D.EURUSD.TODAY.IP")
            assert metadata2 == metadata1
            assert mock_aiohttp_session.request.call_count == 1  # No additional call

            # Force refresh should fetch again
            metadata3 = await client.get_instrument_metadata("CS.D.EURUSD.TODAY.IP", force_refresh=True)
            assert metadata3["dealingRules"]["minStepDistance"]["value"] == 0.1
            assert mock_aiohttp_session.request.call_count == 2

    @pytest.mark.asyncio
    async def test_quantise_size_rounds_down_to_step(self, mock_aiohttp_session):
        """Test that quantise_size properly rounds down to the instrument's step size."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # Populate cache directly (simulating a prior get_instrument_metadata call)
            client._instrument_metadata["CS.D.EURUSD.TODAY.IP"] = {
                "dealingRules": {
                    "minStepDistance": {"value": 0.5}
                }
            }

            # Test various sizes
            assert client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.0) == 1.0
            assert client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.2) == 1.0
            assert client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.5) == 1.5
            assert client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.759110721932789) == 1.5
            assert client.quantise_size("CS.D.EURUSD.TODAY.IP", 2.3) == 2.0
            assert client.quantise_size("CS.D.EURUSD.TODAY.IP", 2.5) == 2.5

    @pytest.mark.asyncio
    async def test_quantise_size_with_decimal_step(self, mock_aiohttp_session):
        """Test quantise_size with a smaller decimal step size."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            client._instrument_metadata["IX.D.DOW.DAILY.IP"] = {
                "dealingRules": {
                    "minStepDistance": {"value": 0.1}
                }
            }

            assert client.quantise_size("IX.D.DOW.DAILY.IP", 1.759110721932789) == 1.7
            assert client.quantise_size("IX.D.DOW.DAILY.IP", 2.35) == 2.3
            assert client.quantise_size("IX.D.DOW.DAILY.IP", 0.05) == 0.0

    @pytest.mark.asyncio
    async def test_quantise_size_no_step_returns_original(self, mock_aiohttp_session):
        """Test that quantise_size returns original size when no step is defined."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # No minStepDistance defined
            client._instrument_metadata["TEST.EPIC"] = {
                "dealingRules": {}
            }

            size = 1.759110721932789
            assert client.quantise_size("TEST.EPIC", size) == size

    @pytest.mark.asyncio
    async def test_quantise_size_raises_without_metadata(self, mock_aiohttp_session):
        """Test that quantise_size raises RuntimeError if metadata not loaded."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="Instrument metadata not loaded"):
                client.quantise_size("UNKNOWN.EPIC", 1.0)
