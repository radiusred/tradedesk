# tests/test_strategy.py
"""
Tests for the BaseStrategy class.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, Mock, patch, PropertyMock
import pytest
from tradedesk.strategy import BaseStrategy
from tradedesk.subscriptions import MarketSubscription

class TestBaseStrategy:
    """Test the BaseStrategy base class."""
    
    def test_initialization(self):
        """Test strategy initialization."""
        mock_client = MagicMock()
        
        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP"), MarketSubscription("CS.D.GBPUSD.CFD.IP")]
            
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                pass
        
        strategy = TestStrategy(mock_client)
        
        assert strategy.client == mock_client
        assert strategy.POLL_INTERVAL == 5
        assert strategy.watchdog_threshold == 60
        assert isinstance(strategy.last_update, datetime)
    
    @pytest.mark.asyncio
    async def test_run_without_lightstreamer(self):
        """Test strategy falls back to polling when Lightstreamer unavailable."""
        mock_client = MagicMock()
        mock_client.get_market_snapshot = AsyncMock(return_value={
            "snapshot": {
                "bid": 1.2345,
                "offer": 1.2347
            }
        })
        
        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP")]
            
            def __init__(self, client):
                super().__init__(client)
                self.updates = []
            
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                self.updates.append((epic, bid, offer))
        
        strategy = TestStrategy(mock_client)
        
        # Mock _has_streamer() to return False
        with patch.object(strategy, '_has_streamer', return_value=False):
            # Run for a short time then cancel
            task = asyncio.create_task(strategy.run())
            await asyncio.sleep(0.1)
            task.cancel()
            
            try:
                await task
            except asyncio.CancelledError:
                pass
            
            # Should have called get_market_snapshot at least once
            mock_client.get_market_snapshot.assert_called_with("CS.D.EURUSD.CFD.IP")
            
            # Should have received updates
            assert len(strategy.updates) > 0
    
    @pytest.mark.asyncio
    async def test_polling_mode(self):
        """Test polling mode functionality."""
        mock_client = MagicMock()
        
        updates = []
        
        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP"), MarketSubscription("CS.D.GBPUSD.CFD.IP")]
            
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                updates.append({
                    'epic': epic,
                    'bid': bid,
                    'offer': offer
                })
        
        strategy = TestStrategy(mock_client)
        strategy.POLL_INTERVAL = 0.1  # Fast polling for test
        
        # Mock different snapshots for each EPIC
        mock_client.get_market_snapshot = AsyncMock(side_effect=[
            {"snapshot": {"bid": 1.1000, "offer": 1.1002}},  # First EURUSD
            {"snapshot": {"bid": 1.3000, "offer": 1.3002}},  # First GBPUSD
            {"snapshot": {"bid": 1.1001, "offer": 1.1003}},  # Second EURUSD (changed)
            {"snapshot": {"bid": 1.3000, "offer": 1.3002}},  # Second GBPUSD (same)
        ])
        
        # Run polling for a short time
        task = asyncio.create_task(strategy._run_polling())
        await asyncio.sleep(0.25)  # Enough for two polling cycles
        task.cancel()
        
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        # Should have received updates for price changes only
        # EURUSD changed, GBPUSD didn't change on second poll
        assert len(updates) == 3  # First EURUSD, First GBPUSD, Second EURUSD
        
        # Verify EPIC order in updates
        assert updates[0]['epic'] == "CS.D.EURUSD.CFD.IP"
        assert updates[1]['epic'] == "CS.D.GBPUSD.CFD.IP"
        assert updates[2]['epic'] == "CS.D.EURUSD.CFD.IP"
    
    @pytest.mark.asyncio
    async def test_polling_mode_exception_handling(self):
        """Test polling mode handles exceptions gracefully."""
        mock_client = MagicMock()
        
        updates = []
        
        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP")]
            
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                updates.append('update')
        
        strategy = TestStrategy(mock_client)
        strategy.POLL_INTERVAL = 0.1
        
        # Mock an exception on first call, success on second
        mock_client.get_market_snapshot = AsyncMock(side_effect=[
            Exception("Network error"),
            {"snapshot": {"bid": 1.1000, "offer": 1.1002}}
        ])
        
        with patch('logging.Logger.exception') as mock_exception:
            task = asyncio.create_task(strategy._run_polling())
            await asyncio.sleep(0.15)
            task.cancel()
            
            try:
                await task
            except asyncio.CancelledError:
                pass
            
            # Exception should have been logged
            mock_exception.assert_called_once()
            # But polling should continue and eventually succeed
            assert len(updates) == 1
    
    @pytest.mark.asyncio
    async def test_streaming_mode_setup(self, mock_lightstreamer):
        """Test Lightstreamer streaming mode setup."""
        mock_client = MagicMock()
        mock_client.ls_url = "https://demo-apd.marketdatasystems.com"
        mock_client.ls_cst = "CST_TOKEN"
        mock_client.ls_xst = "XST_TOKEN"
        mock_client.client_id = "CLIENT123"
        mock_client.account_id = "ACCOUNT123"  # fallback path used in streamer

        # Provide the streamer via the new client API
        import tradedesk.providers.ig.streamer as ig_streamer

        mock_client.get_streamer = lambda: ig_streamer.Lightstreamer(mock_client)

        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP")]

            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                pass

        strategy = TestStrategy(mock_client)

        mock_ls_info = mock_lightstreamer

        # Patch the Lightstreamer module (not tradedesk.strategy)
        ig_streamer.LightstreamerClient = lambda *args, **kwargs: mock_ls_info["client"]
        ig_streamer.Subscription = lambda *args, **kwargs: mock_ls_info["subscription"]

        # Run streaming for a moment then cancel
        task = asyncio.create_task(strategy._run_streaming())
        await asyncio.sleep(0.1)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify Lightstreamer client was configured
        mock_ls_info["client"].connectionDetails.setUser.assert_called_with("CLIENT123")
        mock_ls_info["client"].connectionDetails.setPassword.assert_called_with(
            "CST-CST_TOKEN|XST-XST_TOKEN"
        )
        mock_ls_info["client"].connect.assert_called_once()
        mock_ls_info["subscription"].addListener.assert_called_once()
        mock_ls_info["client"].subscribe.assert_called_once()

class TestConcreteStrategy:
    """Test with a concrete strategy implementation."""
    
    @pytest.mark.asyncio
    async def test_complete_strategy_lifecycle(self):
        """Test a complete strategy from initialization to shutdown."""
        mock_client = MagicMock()
        
        class SimpleStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP")]
            
            def __init__(self, client):
                super().__init__(client)
                self.started = False
                self.stopped = False
                self.updates = []
            
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                self.updates.append({
                    'epic': epic,
                    'bid': bid,
                    'offer': offer
                })
            
            async def run(self):
                self.started = True
                try:
                    await asyncio.Future()  # Run forever
                except asyncio.CancelledError:
                    self.stopped = True
                    raise
        
        strategy = SimpleStrategy(mock_client)
        
        # Run the strategy briefly
        task = asyncio.create_task(strategy.run())
        await asyncio.sleep(0.1)
        
        assert strategy.started is True
        assert strategy.stopped is False
        
        # Cancel the strategy
        task.cancel()
        
        try:
            await task
        except asyncio.CancelledError:
            pass
        
        assert strategy.stopped is True
