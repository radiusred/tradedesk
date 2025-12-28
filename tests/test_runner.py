# tests/test_runner.py
"""
Tests for the runner module.
"""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch, call
import pytest
from tradedesk.runner import run_strategies, configure_logging, _run_strategies_async
from tradedesk.strategy import BaseStrategy

class TestRunner:
    """Test the strategy runner."""
    
    def test_configure_logging_defaults(self):
        """Test logging configuration works when no handlers exist."""
        import logging
        
        # Clear existing handlers to simulate clean slate
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        
        configure_logging("DEBUG")
        
        # Should have added a handler
        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0], logging.StreamHandler)

    def test_configure_logging_respects_existing(self):
        """Test logging configuration does NOT overwrite existing handlers."""
        import logging
        
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        
        # Manually add a handler
        dummy_handler = logging.NullHandler()
        root_logger.addHandler(dummy_handler)
        root_logger.setLevel(logging.WARNING)
        
        # Try to configure logging (should perform no-op)
        configure_logging("DEBUG")
        
        # Level should NOT have changed to DEBUG
        assert root_logger.level == logging.WARNING
        # Should NOT have added a StreamHandler
        assert len(root_logger.handlers) == 1
        assert root_logger.handlers[0] == dummy_handler

    def test_configure_logging_force(self):
        """Test logging configuration CAN force overwrite."""
        import logging
        
        root_logger = logging.getLogger()
        root_logger.handlers.clear()
        
        # Manually add a handler
        root_logger.addHandler(logging.NullHandler())
        
        # Force configure
        configure_logging("DEBUG", force=True)
        
        # Should have wiped NullHandler and added StreamHandler
        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0], logging.StreamHandler)
    
    @pytest.mark.asyncio
    async def test_run_strategies_async_empty(self):
        """Test running empty strategy list."""
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        
        with patch('tradedesk.runner.log.warning') as mock_warning:
            await _run_strategies_async([], mock_client)
            
            mock_warning.assert_called_with("No strategies to run")
    
    @pytest.mark.asyncio
    async def test_run_strategies_async_single(self):
        """Test running a single strategy."""
        mock_strategy = MagicMock()
        mock_strategy.subscriptions = [SimpleNamespace(epic="CS.D.EURUSD.CFD.IP")]
        mock_strategy.__class__.__name__ = "TestStrategy"
        mock_strategy.run = AsyncMock()
        
        # Create a proper async mock for client
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        
        with patch('tradedesk.runner.log.info') as mock_info:
            await _run_strategies_async([mock_strategy], mock_client)
            
            # Verify logging - check the actual call format
            expected_calls = [
                call(
                    "Loaded %s monitoring %d EPIC%s: %s",
                    "TestStrategy",
                    1,
                    "",
                    "CS.D.EURUSD.CFD.IP"
                )
            ]
            
            # Check if any call matches our expected format
            found = False
            for actual_call in mock_info.call_args_list:
                try:
                    # Check if this is the "Loaded ..." call
                    if (len(actual_call[0]) >= 5 and 
                        actual_call[0][0] == "Loaded %s monitoring %d EPIC%s: %s" and
                        actual_call[0][1] == "TestStrategy" and
                        actual_call[0][2] == 1):
                        found = True
                        break
                except (IndexError, AttributeError):
                    continue
            
            assert found, "Expected log call not found"
            
            # Verify strategy was run
            mock_strategy.run.assert_awaited_once()
    
    @pytest.mark.asyncio
    async def test_run_strategies_async_multiple(self):
        """Test running multiple strategies."""
        mock_strategy1 = MagicMock()
        mock_strategy1.subscriptions = [SimpleNamespace(epic="CS.D.EURUSD.CFD.IP")]
        mock_strategy1.__class__.__name__ = "Strategy1"
        mock_strategy1.run = AsyncMock()
        
        mock_strategy2 = MagicMock()
        mock_strategy2.subscriptions = [SimpleNamespace(epic="CS.D.GBPUSD.CFD.IP")]
        mock_strategy2.__class__.__name__ = "Strategy2"
        mock_strategy2.run = AsyncMock()
        
        # Create a proper async mock for client
        mock_client = AsyncMock()
        mock_client.close = AsyncMock()
        
        with patch('tradedesk.runner.log.info') as mock_info:
            await _run_strategies_async([mock_strategy1, mock_strategy2], mock_client)
            
            # Both strategies should have been run
            mock_strategy1.run.assert_awaited_once()
            mock_strategy2.run.assert_awaited_once()
            
            # Check logging - simpler approach
            call_strings = [str(call_obj) for call_obj in mock_info.call_args_list]
            
            # Look for our expected calls in the call strings
            found_strategy1 = False
            found_strategy2 = False
            
            for call_str in call_strings:
                if "Strategy1" in call_str and "CS.D.EURUSD.CFD.IP" in call_str:
                    found_strategy1 = True
                if "Strategy2" in call_str and "CS.D.GBPUSD.CFD.IP" in call_str:
                    found_strategy2 = True
            
            assert found_strategy1, "Expected log for Strategy1 not found"
            assert found_strategy2, "Expected log for Strategy2 not found"
    
    def test_run_strategies_invalid_config(self):
        """Runner exits if a strategy fails to instantiate, and closes the client."""
        class BoomStrategy(BaseStrategy):
            def __init__(self, client, **kwargs):
                super().__init__(client, **kwargs)
                raise ValueError("Missing credentials")

            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                pass

        client = MagicMock()
        client.start = AsyncMock()
        client.close = AsyncMock()

        with patch("sys.exit") as mock_exit:
            run_strategies(
                strategy_specs=[BoomStrategy],
                client_factory=lambda: client,
                setup_logging=False,
            )

            mock_exit.assert_called_with(1)
            client.start.assert_awaited_once()
            client.close.assert_awaited_once()


    def test_run_strategies_client_error(self):
        """Runner exits if a strategy run task errors, and closes the client."""
        class ErrorStrategy(BaseStrategy):
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                pass

            async def run(self):
                raise Exception("Auth failed")

        client = MagicMock()
        client.start = AsyncMock()
        client.close = AsyncMock()

        with patch("sys.exit") as mock_exit:
            run_strategies(
                strategy_specs=[ErrorStrategy],
                client_factory=lambda: client,
                setup_logging=False,
            )

            mock_exit.assert_called_with(1)
            client.start.assert_awaited_once()
            client.close.assert_awaited_once()

    def test_run_strategies_keyboard_interrupt(self):
        """Test graceful handling of KeyboardInterrupt."""
        class MockStrategy(BaseStrategy):
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                pass

        client = MagicMock()
        client.close = AsyncMock()

        # Force asyncio.run(...) in runner to raise KeyboardInterrupt
        with patch("asyncio.run", side_effect=KeyboardInterrupt()):
            with patch("logging.Logger.info") as mock_info:
                run_strategies(client, [MockStrategy], setup_logging=False)

                mock_info.assert_any_call("Interrupted by user - shutting down gracefully")
