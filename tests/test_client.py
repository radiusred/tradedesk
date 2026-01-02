# tests/test_client.py
"""
Tests for the IGClient class.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from tradedesk.providers.ig.client import IGClient

class TestIGClient:
    """Test the IGClient class."""
    
    @pytest.mark.asyncio
    async def test_initialization_demo(self):
        """Test client initialization for DEMO environment."""
        client = IGClient()
        
        assert client.base_url == "https://demo-api.ig.com/gateway/deal"
        assert client.ls_url == "https://demo-apd.marketdatasystems.com"
        assert client.headers["VERSION"] == "2"
        assert client._auth_lock is not None
    
    @pytest.mark.asyncio
    async def test_initialization_live(self):
        """Test client initialization for LIVE environment."""
        with patch('tradedesk.providers.ig.client.settings') as mock_settings:
            mock_settings.environment = "LIVE"
            mock_settings.ig_api_key = "test-key"
            mock_settings.ig_username = "test-user"
            mock_settings.ig_password = "test-pass"
            
            client = IGClient()
            
            assert client.base_url == "https://api.ig.com/gateway/deal"
            assert client.ls_url == "https://apd.marketdatasystems.com"
            assert client.headers["VERSION"] == "2"
    
    @pytest.mark.asyncio
    async def test_start_and_close(self, mock_aiohttp_session):
        """Test starting and closing the client."""
        with patch('aiohttp.ClientSession', return_value=mock_aiohttp_session):
            client = IGClient()
            await client.start()
            
            # Verify session was created
            assert client._session is not None
            
            # Close the client
            await client.close()
    
    @pytest.mark.asyncio
    async def test_context_manager(self, mock_aiohttp_session):
        """Test client as async context manager."""
        with patch('aiohttp.ClientSession', return_value=mock_aiohttp_session):
            async with IGClient() as client:
                assert client._session is not None
    
    @pytest.mark.asyncio
    async def test_authentication_success(self, mock_aiohttp_session):
        """Test successful authentication."""
        with patch('aiohttp.ClientSession', return_value=mock_aiohttp_session):
            client = IGClient()
            await client.start()  # This calls _authenticate
            
            # Verify tokens were stored
            assert client.ls_cst == "CST_TOKEN"
            assert client.ls_xst == "XST_TOKEN"
            assert client.account_id == "ACC456"
            assert client.client_id == "CLIENT123"
            assert client.uses_oauth is False
    
    @pytest.mark.asyncio
    async def test_get_market_snapshot(self, mock_aiohttp_session):
        """Test getting market snapshot."""
        # Create a mock response with market data
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={
            "snapshot": {"bid": 1.2345, "offer": 1.2347}
        })
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        
        mock_aiohttp_session.request.return_value = mock_response
        
        with patch('aiohttp.ClientSession', return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session
            
            result = await client.get_market_snapshot("CS.D.EURUSD.CFD.IP")
            
            assert result["snapshot"]["bid"] == 1.2345
            assert result["snapshot"]["offer"] == 1.2347
    
    @pytest.mark.asyncio
    async def test_place_market_order(self, mock_aiohttp_session):
        """Test placing a market order."""
        # Create a mock response with order confirmation
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"dealReference": "REF123"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=None)
        
        mock_aiohttp_session.request.return_value = mock_response
        
        with patch('aiohttp.ClientSession', return_value=mock_aiohttp_session):
            client = IGClient()
            client._session = mock_aiohttp_session
            
            result = await client.place_market_order(
                epic="CS.D.EURUSD.CFD.IP",
                direction="BUY",
                size=1.0,
                currency="USD"
            )
            
            assert result == {"dealReference": "REF123"}
    
    def test_token_validation(self):
        """Test OAuth token validation."""
        client = IGClient()
        client.uses_oauth = True
        
        # Token with future expiry (valid)
        client.oauth_expires_at = time.time() + 100
        assert client._is_token_valid() is True
        
        # Token with past expiry (invalid)
        client.oauth_expires_at = time.time() - 100
        assert client._is_token_valid() is False
        
        # Non-OAuth auth (always valid)
        client.uses_oauth = False
        assert client._is_token_valid() is True
