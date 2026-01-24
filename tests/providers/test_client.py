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
            async def fake_request(_method, _path, **_kwargs):
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
            # minDealSize of 0.5 means step of 0.1 (1 decimal place)
            client._instrument_metadata["CS.D.EURUSD.TODAY.IP"] = {
                "dealingRules": {
                    "minDealSize": {"value": 0.5}
                }
            }

            # Test various sizes with step=0.1
            assert await client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.0) == 1.0
            assert await client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.24) == 1.2
            assert await client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.55) == 1.5
            assert await client.quantise_size("CS.D.EURUSD.TODAY.IP", 1.759110721932789) == 1.7
            assert await client.quantise_size("CS.D.EURUSD.TODAY.IP", 2.39) == 2.3
            assert await client.quantise_size("CS.D.EURUSD.TODAY.IP", 2.5) == 2.5

    @pytest.mark.asyncio
    async def test_quantise_size_with_decimal_step(self, mock_aiohttp_session):
        """Test quantise_size with a smaller decimal step size."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # minDealSize of 0.01 means step of 0.01 (2 decimal places)
            client._instrument_metadata["IX.D.DOW.DAILY.IP"] = {
                "dealingRules": {
                    "minDealSize": {"value": 0.01}
                }
            }

            assert await client.quantise_size("IX.D.DOW.DAILY.IP", 1.759110721932789) == 1.75
            assert await client.quantise_size("IX.D.DOW.DAILY.IP", 2.357) == 2.35
            assert await client.quantise_size("IX.D.DOW.DAILY.IP", 0.055) == 0.05

    @pytest.mark.asyncio
    async def test_quantise_size_no_step_returns_original(self, mock_aiohttp_session):
        """Test that quantise_size returns original size when no minDealSize is defined."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # No minDealSize defined
            client._instrument_metadata["TEST.EPIC"] = {
                "dealingRules": {}
            }

            size = 1.759110721932789
            assert await client.quantise_size("TEST.EPIC", size) == size

    @pytest.mark.asyncio
    async def test_quantise_size_fetches_metadata_if_not_cached(self, mock_aiohttp_session):
        """Test that quantise_size fetches metadata if not already cached."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "dealingRules": {
                "minDealSize": {"value": 0.04},
            },
            "instrument": {"epic": "CS.D.AUDUSD.TODAY.IP"},
            "snapshot": {"bid": 0.65, "offer": 0.6501},
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            # Cache should be empty initially
            assert "CS.D.AUDUSD.TODAY.IP" not in client._instrument_metadata

            # quantise_size should fetch metadata
            result = await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.073)

            # minDealSize=0.04 (2 decimals) -> step=0.01
            # 0.073 rounds down to 0.07
            assert result == 0.07

            # Metadata should now be cached
            assert "CS.D.AUDUSD.TODAY.IP" in client._instrument_metadata
            assert mock_aiohttp_session.request.call_count == 1

    @pytest.mark.asyncio
    async def test_quantise_size_with_integer_min_deal_size(self, mock_aiohttp_session):
        """Test quantise_size with integer minDealSize (step should be 1)."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # minDealSize of 1 (integer) means step of 1
            client._instrument_metadata["IX.D.FTSE.DAILY.IP"] = {
                "dealingRules": {
                    "minDealSize": {"value": 1}
                }
            }

            assert await client.quantise_size("IX.D.FTSE.DAILY.IP", 5.7) == 5.0
            assert await client.quantise_size("IX.D.FTSE.DAILY.IP", 10.3) == 10.0
            assert await client.quantise_size("IX.D.FTSE.DAILY.IP", 1.0) == 1.0
            # 0.9 quantises to 0, but minimum is 1, so returns 1
            assert await client.quantise_size("IX.D.FTSE.DAILY.IP", 0.9) == 1.0

    @pytest.mark.asyncio
    async def test_quantise_size_enforces_minimum_deal_size(self, mock_aiohttp_session):
        """Test that quantise_size enforces minimum deal size."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # minDealSize of 0.04 (2 decimals) means step of 0.01
            client._instrument_metadata["CS.D.AUDUSD.TODAY.IP"] = {
                "dealingRules": {
                    "minDealSize": {"value": 0.04}
                }
            }

            # Size below minimum should be set to minimum
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.02) == 0.04
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.03) == 0.04

            # Size at or above minimum should quantise normally
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.04) == 0.04
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.05) == 0.05
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.059) == 0.05
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.06) == 0.06
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.074) == 0.07

    @pytest.mark.asyncio
    async def test_quantise_size_audusd_real_world_example(self, mock_aiohttp_session):
        """Test quantise_size with real AUDUSD parameters from the issue."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # Real AUDUSD parameters: minDealSize=0.04, minStepDistance=5 (ignored)
            client._instrument_metadata["CS.D.AUDUSD.TODAY.IP"] = {
                "dealingRules": {
                    "minDealSize": {"value": 0.04},
                    "minStepDistance": {"value": 5}  # This is ignored in new implementation
                }
            }

            # These should all work (as per the issue, IG web client allows these)
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.04) == 0.04
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.05) == 0.05
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.06) == 0.06
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.10) == 0.10

            # Sizes that need rounding
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.045) == 0.04
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 0.078) == 0.07
            assert await client.quantise_size("CS.D.AUDUSD.TODAY.IP", 1.234) == 1.23

    @pytest.mark.asyncio
    async def test_quantise_size_with_three_decimal_places(self, mock_aiohttp_session):
        """Test quantise_size with minDealSize having 3 decimal places."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # minDealSize of 0.001 (3 decimals) means step of 0.001
            client._instrument_metadata["TEST.CRYPTO"] = {
                "dealingRules": {
                    "minDealSize": {"value": 0.001}
                }
            }

            assert await client.quantise_size("TEST.CRYPTO", 1.2345) == 1.234
            assert await client.quantise_size("TEST.CRYPTO", 0.0056) == 0.005
            assert await client.quantise_size("TEST.CRYPTO", 10.9999) == 10.999

    @pytest.mark.asyncio
    async def test_quantise_size_very_large_numbers(self, mock_aiohttp_session):
        """Test quantise_size with large position sizes."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            # minDealSize of 10 (integer) means step of 1
            client._instrument_metadata["IX.D.DOW.LARGE"] = {
                "dealingRules": {
                    "minDealSize": {"value": 10}
                }
            }

            assert await client.quantise_size("IX.D.DOW.LARGE", 1234.7) == 1234.0
            assert await client.quantise_size("IX.D.DOW.LARGE", 9876.5) == 9876.0
            assert await client.quantise_size("IX.D.DOW.LARGE", 100000.1) == 100000.0
            # Below minimum
            assert await client.quantise_size("IX.D.DOW.LARGE", 5.5) == 10.0

    # ============================================================================
    # Context Manager and Lifecycle Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_context_manager_calls_start_and_close(self, mock_aiohttp_session):
        """Test that async context manager properly initializes and closes the client."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"CST": "test_cst", "X-SECURITY-TOKEN": "test_xst"}
        mock_response.json = AsyncMock(return_value={
            "accountId": "ACC123",
            "clientId": "CLIENT123",
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            async with IGClient() as client:
                assert client._session is not None
                assert client.account_id == "ACC123"

            # After exiting context, session should be closed
            assert mock_aiohttp_session.close.called

    @pytest.mark.asyncio
    async def test_start_creates_session_and_authenticates(self, mock_aiohttp_session):
        """Test that start() creates a session and authenticates."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"CST": "test_cst", "X-SECURITY-TOKEN": "test_xst"}
        mock_response.json = AsyncMock(return_value={
            "accountId": "ACC123",
            "clientId": "CLIENT123",
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            assert client._session is None

            await client.start()

            assert client._session is not None
            assert client.account_id == "ACC123"

    @pytest.mark.asyncio
    async def test_close_closes_session(self, mock_aiohttp_session):
        """Test that close() properly closes the aiohttp session."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            await client.close()

            assert mock_aiohttp_session.close.called
            assert client._session is None

    # ============================================================================
    # Authentication Error Handling Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_auth_handles_rate_limit_error(self, mock_aiohttp_session):
        """Test that authentication properly handles rate limit errors."""
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.json = AsyncMock(return_value={
            "errorCode": "error.public-api.exceeded-api-key-allowance"
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="IG API rate limit exceeded"):
                await client.start()

    @pytest.mark.asyncio
    async def test_auth_handles_invalid_credentials(self, mock_aiohttp_session):
        """Test that authentication properly handles invalid credentials."""
        mock_response = MagicMock()
        mock_response.status = 401
        mock_response.json = AsyncMock(return_value={"error": "Invalid credentials"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="IG authentication failed – HTTP 401"):
                await client.start()

    @pytest.mark.asyncio
    async def test_auth_handles_network_error(self, mock_aiohttp_session):
        """Test that authentication properly handles network errors."""
        import aiohttp
        mock_aiohttp_session.post.side_effect = aiohttp.ClientError("Network unreachable")

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="Network error during authentication"):
                await client.start()

    @pytest.mark.asyncio
    async def test_auth_handles_non_json_error_response(self, mock_aiohttp_session):
        """Test that authentication handles non-JSON error responses."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.json = AsyncMock(side_effect=Exception("Not JSON"))
        mock_response.text = AsyncMock(return_value="Internal Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="IG authentication failed – HTTP 500"):
                await client.start()

    # ============================================================================
    # OAuth and V3 Authentication Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_v3_auth_stores_oauth_tokens(self, mock_aiohttp_session):
        """Test that V3 authentication properly stores OAuth tokens."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.json = AsyncMock(return_value={
            "oauthToken": {
                "access_token": "test_access_token",
                "refresh_token": "test_refresh_token",
                "expires_in": 60
            },
            "accountId": "ACC123",
            "clientId": "CLIENT123",
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            with patch("tradedesk.providers.ig.client.settings") as mock_settings:
                mock_settings.ig_environment = "DEMO"
                mock_settings.ig_api_key = "test-key"
                mock_settings.ig_username = "test-user"
                mock_settings.ig_password = "test-pass"

                client = IGClient()
                client.api_version = "3"

                await client.start()

                assert client.uses_oauth is True
                assert client.oauth_access_token == "test_access_token"
                assert client.oauth_refresh_token == "test_refresh_token"
                assert client.account_id == "ACC123"

    @pytest.mark.asyncio
    async def test_v3_auth_raises_without_oauth_token(self, mock_aiohttp_session):
        """Test that V3 authentication raises error without OAuth token."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}
        mock_response.json = AsyncMock(return_value={
            "accountId": "ACC123",
            "clientId": "CLIENT123",
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            with patch("tradedesk.providers.ig.client.settings") as mock_settings:
                mock_settings.ig_environment = "DEMO"
                mock_settings.ig_api_key = "test-key"
                mock_settings.ig_username = "test-user"
                mock_settings.ig_password = "test-pass"

                client = IGClient()
                client.api_version = "3"

                with pytest.raises(RuntimeError, match="OAuth access_token not found"):
                    await client.start()

    @pytest.mark.asyncio
    async def test_request_refreshes_expired_oauth_token(self, mock_aiohttp_session):
        """Test that _request() refreshes expired OAuth tokens."""
        # Setup auth response
        auth_response = MagicMock()
        auth_response.status = 200
        auth_response.headers = {}
        auth_response.json = AsyncMock(return_value={
            "oauthToken": {
                "access_token": "new_access_token",
                "refresh_token": "new_refresh_token",
                "expires_in": 60
            },
            "accountId": "ACC123",
            "clientId": "CLIENT123",
        })
        auth_response.__aenter__ = AsyncMock(return_value=auth_response)
        auth_response.__aexit__ = AsyncMock(return_value=None)

        # Setup request response
        request_response = MagicMock()
        request_response.status = 200
        request_response.json = AsyncMock(return_value={"data": "test"})
        request_response.__aenter__ = AsyncMock(return_value=request_response)
        request_response.__aexit__ = AsyncMock(return_value=None)

        # First call for auth, second for actual request
        mock_aiohttp_session.post.return_value = auth_response
        mock_aiohttp_session.request.return_value = request_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            with patch("tradedesk.providers.ig.client.settings") as mock_settings:
                mock_settings.ig_environment = "DEMO"
                mock_settings.ig_api_key = "test-key"
                mock_settings.ig_username = "test-user"
                mock_settings.ig_password = "test-pass"

                client = IGClient()
                client.api_version = "3"
                await client.start()

                # Expire the token
                client.oauth_expires_at = time.time() - 100
                client.last_auth_attempt = time.time() - 30

                # Make a request - should trigger re-auth
                result = await client._request("GET", "/test")

                assert result == {"data": "test"}
                # Should have called post for re-auth
                assert mock_aiohttp_session.post.call_count >= 2

    # ============================================================================
    # Request and Retry Logic Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_request_with_custom_api_version(self, mock_aiohttp_session):
        """Test that _request() can use custom API version."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"data": "test"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            await client._request("GET", "/test", api_version="3")

            # Check that VERSION header was set to "3"
            call_args = mock_aiohttp_session.request.call_args
            assert call_args[1]["headers"]["VERSION"] == "3"

    @pytest.mark.asyncio
    async def test_request_handles_http_errors(self, mock_aiohttp_session):
        """Test that _request() properly handles HTTP errors."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.json = AsyncMock(return_value={"error": "Internal error"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            with pytest.raises(RuntimeError, match="IG request failed: HTTP 500"):
                await client._request("GET", "/test")

    @pytest.mark.asyncio
    async def test_request_handles_non_json_error_response(self, mock_aiohttp_session):
        """Test that _request() handles non-JSON error responses."""
        mock_response = MagicMock()
        mock_response.status = 500
        mock_response.json = AsyncMock(side_effect=Exception("Not JSON"))
        mock_response.text = AsyncMock(return_value="Internal Server Error")
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            with pytest.raises(RuntimeError, match="IG request failed: HTTP 500"):
                await client._request("GET", "/test")

    # ============================================================================
    # Utility Method Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_get_market_snapshot(self, mock_aiohttp_session):
        """Test get_market_snapshot returns market data."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "snapshot": {"bid": 1.1, "offer": 1.1001},
            "dealingRules": {"minDealSize": {"value": 0.5}},
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            result = await client.get_market_snapshot("CS.D.EURUSD.TODAY.IP")

            assert result["snapshot"]["bid"] == 1.1
            assert result["dealingRules"]["minDealSize"]["value"] == 0.5

    @pytest.mark.asyncio
    async def test_get_price_ticks(self, mock_aiohttp_session):
        """Test get_price_ticks returns price tick data."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "prices": [{"bid": 1.1, "ask": 1.1001}]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            result = await client.get_price_ticks("CS.D.EURUSD.TODAY.IP")

            assert result["prices"][0]["bid"] == 1.1

    def test_period_to_rest_resolution(self):
        """Test period string mapping to IG REST resolution."""
        client = IGClient()

        assert client._period_to_rest_resolution("1MINUTE") == "MINUTE"
        assert client._period_to_rest_resolution("5MINUTE") == "MINUTE_5"
        assert client._period_to_rest_resolution("15MINUTE") == "MINUTE_15"
        assert client._period_to_rest_resolution("30MINUTE") == "MINUTE_30"
        assert client._period_to_rest_resolution("HOUR") == "HOUR"
        assert client._period_to_rest_resolution("4HOUR") == "HOUR_4"
        assert client._period_to_rest_resolution("DAY") == "DAY"
        assert client._period_to_rest_resolution("WEEK") == "WEEK"

        # Test passthrough
        assert client._period_to_rest_resolution("MINUTE") == "MINUTE"
        assert client._period_to_rest_resolution("UNKNOWN") == "UNKNOWN"

    @pytest.mark.asyncio
    async def test_ensure_account_type_caches_result(self, mock_aiohttp_session):
        """Test that _ensure_account_type caches the account type."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "accounts": [
                {"accountId": "ACC123", "accountType": "CFD"},
                {"accountId": "ACC456", "accountType": "SPREADBET"},
            ]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session
            client.account_id = "ACC123"

            # First call should fetch from API
            account_type1 = await client._ensure_account_type()
            assert account_type1 == "CFD"
            assert mock_aiohttp_session.request.call_count == 1

            # Second call should use cache
            account_type2 = await client._ensure_account_type()
            assert account_type2 == "CFD"
            assert mock_aiohttp_session.request.call_count == 1  # No additional call

    @pytest.mark.asyncio
    async def test_get_streamer_returns_lightstreamer(self):
        """Test that get_streamer returns a Lightstreamer instance."""
        client = IGClient()

        with patch("tradedesk.providers.ig.streamer.Lightstreamer") as mock_ls:
            _ = client.get_streamer()

            mock_ls.assert_called_once_with(client)

    @pytest.mark.asyncio
    async def test_place_market_order_confirmed_waits_for_confirmation(self, mock_aiohttp_session):
        """Test place_market_order_confirmed waits for deal confirmation."""
        # Setup order placement response
        order_response = MagicMock()
        order_response.status = 200
        order_response.json = AsyncMock(return_value={"dealReference": "REF123"})
        order_response.__aenter__ = AsyncMock(return_value=order_response)
        order_response.__aexit__ = AsyncMock(return_value=None)

        # Setup confirmation response
        confirm_response = MagicMock()
        confirm_response.status = 200
        confirm_response.json = AsyncMock(return_value={
            "dealStatus": "ACCEPTED",
            "dealId": "DEAL123"
        })
        confirm_response.__aenter__ = AsyncMock(return_value=confirm_response)
        confirm_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.side_effect = [order_response, confirm_response]

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            result = await client.place_market_order_confirmed(
                epic="CS.D.EURUSD.TODAY.IP",
                direction="BUY",
                size=1.0,
                confirm_timeout_s=1.0,
                confirm_poll_s=0.0
            )

            assert result["dealStatus"] == "ACCEPTED"
            assert result["dealId"] == "DEAL123"

    @pytest.mark.asyncio
    async def test_place_market_order_confirmed_raises_without_deal_reference(self, mock_aiohttp_session):
        """Test place_market_order_confirmed raises error without dealReference."""
        # Setup order placement response without dealReference
        order_response = MagicMock()
        order_response.status = 200
        order_response.json = AsyncMock(return_value={"error": "something went wrong"})
        order_response.__aenter__ = AsyncMock(return_value=order_response)
        order_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = order_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            with pytest.raises(RuntimeError, match="Expected dealReference"):
                await client.place_market_order_confirmed(
                    epic="CS.D.EURUSD.TODAY.IP",
                    direction="BUY",
                    size=1.0
                )

    # ============================================================================
    # Historical Candles Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_get_historical_candles_returns_candles(self, mock_aiohttp_session):
        """Test get_historical_candles returns properly formatted candles."""
        from tradedesk.marketdata import Candle

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "prices": [
                {
                    "snapshotTimeUTC": "2025-01-01T10:00:00",
                    "openPrice": {"bid": 1.1000, "ask": 1.1002},
                    "highPrice": {"bid": 1.1010, "ask": 1.1012},
                    "lowPrice": {"bid": 1.0990, "ask": 1.0992},
                    "closePrice": {"bid": 1.1005, "ask": 1.1007},
                    "lastTradedVolume": 1000
                },
                {
                    "snapshotTimeUTC": "2025-01-01T11:00:00Z",
                    "openPrice": {"bid": 1.1005, "ask": 1.1007},
                    "highPrice": {"bid": 1.1020, "ask": 1.1022},
                    "lowPrice": {"bid": 1.1000, "ask": 1.1002},
                    "closePrice": {"bid": 1.1015, "ask": 1.1017},
                    "lastTradedVolume": 1500
                }
            ]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 2)

            assert len(candles) == 2
            assert isinstance(candles[0], Candle)
            assert candles[0].timestamp == "2025-01-01T10:00:00Z"
            assert candles[0].open == 1.1001  # mid price
            assert candles[0].close == 1.1006  # mid price
            assert candles[0].volume == 1000.0

            assert candles[1].timestamp == "2025-01-01T11:00:00Z"
            assert candles[1].close == 1.1016

    @pytest.mark.asyncio
    async def test_get_historical_candles_returns_empty_for_zero_points(self, mock_aiohttp_session):
        """Test get_historical_candles returns empty list for 0 or negative points."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 0)
            assert candles == []

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", -5)
            assert candles == []

    @pytest.mark.asyncio
    async def test_get_historical_candles_handles_missing_timestamps(self, mock_aiohttp_session):
        """Test get_historical_candles skips candles without timestamps."""

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "prices": [
                {
                    # Missing timestamp
                    "openPrice": {"bid": 1.1000, "ask": 1.1002},
                    "closePrice": {"bid": 1.1005, "ask": 1.1007},
                },
                {
                    "snapshotTimeUTC": "2025-01-01T11:00:00Z",
                    "closePrice": {"bid": 1.1015, "ask": 1.1017},
                }
            ]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 2)

            # Should only have 1 candle (the one with timestamp)
            assert len(candles) == 1
            assert candles[0].timestamp == "2025-01-01T11:00:00Z"

    @pytest.mark.asyncio
    async def test_get_historical_candles_handles_missing_close_price(self, mock_aiohttp_session):
        """Test get_historical_candles skips candles without close price."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "prices": [
                {
                    "snapshotTimeUTC": "2025-01-01T10:00:00Z",
                    "openPrice": {"bid": 1.1000, "ask": 1.1002},
                    # Missing closePrice
                },
                {
                    "snapshotTimeUTC": "2025-01-01T11:00:00Z",
                    "closePrice": {"bid": 1.1015, "ask": 1.1017},
                }
            ]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 2)

            # Should only have 1 candle
            assert len(candles) == 1
            assert candles[0].close == 1.1016

    @pytest.mark.asyncio
    async def test_get_historical_candles_uses_close_for_missing_ohlc(self, mock_aiohttp_session):
        """Test get_historical_candles uses close price when open/high/low missing."""

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "prices": [
                {
                    "snapshotTimeUTC": "2025-01-01T10:00:00Z",
                    # Only close price available
                    "closePrice": {"bid": 1.1005, "ask": 1.1007},
                }
            ]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 1)

            assert len(candles) == 1
            # All OHLC should equal close
            assert candles[0].open == 1.1006
            assert candles[0].high == 1.1006
            assert candles[0].low == 1.1006
            assert candles[0].close == 1.1006

    @pytest.mark.asyncio
    async def test_get_historical_candles_sorts_by_timestamp(self, mock_aiohttp_session):
        """Test get_historical_candles sorts candles oldest to newest."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "prices": [
                {
                    "snapshotTimeUTC": "2025-01-01T12:00:00Z",
                    "closePrice": {"bid": 1.1025, "ask": 1.1027},
                },
                {
                    "snapshotTimeUTC": "2025-01-01T10:00:00Z",
                    "closePrice": {"bid": 1.1005, "ask": 1.1007},
                },
                {
                    "snapshotTimeUTC": "2025-01-01T11:00:00Z",
                    "closePrice": {"bid": 1.1015, "ask": 1.1017},
                }
            ]
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 3)

            # Should be sorted oldest to newest
            assert candles[0].timestamp == "2025-01-01T10:00:00Z"
            assert candles[1].timestamp == "2025-01-01T11:00:00Z"
            assert candles[2].timestamp == "2025-01-01T12:00:00Z"

    @pytest.mark.asyncio
    async def test_get_historical_candles_handles_empty_prices(self, mock_aiohttp_session):
        """Test get_historical_candles handles empty prices array."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"prices": []})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.request.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            candles = await client.get_historical_candles("CS.D.EURUSD.TODAY.IP", "1MINUTE", 10)
            assert candles == []

    # ============================================================================
    # Additional Auth and Error Handling Tests
    # ============================================================================

    @pytest.mark.asyncio
    async def test_v2_auth_raises_without_cst_token(self, mock_aiohttp_session):
        """Test V2 authentication raises error without CST token."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {}  # Missing CST
        mock_response.json = AsyncMock(return_value={
            "accountId": "ACC123",
            "clientId": "CLIENT123",
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="CST and X-SECURITY-TOKEN not found"):
                await client.start()

    @pytest.mark.asyncio
    async def test_v2_auth_raises_without_account_id(self, mock_aiohttp_session):
        """Test V2 authentication raises error without account ID."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.headers = {"CST": "test_cst", "X-SECURITY-TOKEN": "test_xst"}
        mock_response.json = AsyncMock(return_value={
            "clientId": "CLIENT123",
            # Missing accountId
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)

        mock_aiohttp_session.post.return_value = mock_response

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            with pytest.raises(RuntimeError, match="IG account id not found"):
                await client.start()

    @pytest.mark.asyncio
    async def test_ensure_account_type_returns_none_without_account_id(self, mock_aiohttp_session):
        """Test _ensure_account_type returns None when account_id is not set."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session
            client.account_id = None

            result = await client._ensure_account_type()
            assert result is None

    @pytest.mark.asyncio
    async def test_dealing_path_for_current_account(self, mock_aiohttp_session):
        """Test _dealing_path_for_current_account returns correct path."""
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()

            path = await client._dealing_path_for_current_account()
            assert path == "/positions/otc"

    @pytest.mark.asyncio
    async def test_handle_retry_logic_on_rate_limit(self, mock_aiohttp_session):
        """Test _handle_retry_logic raises on rate limit error."""
        mock_response = MagicMock()
        mock_response.status = 403
        mock_response.json = AsyncMock(return_value={
            "errorCode": "error.public-api.exceeded-api-key-allowance"
        })

        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session

            with pytest.raises(RuntimeError, match="IG API rate limit exceeded"):
                await client._handle_retry_logic(mock_response, "GET", "/test")
