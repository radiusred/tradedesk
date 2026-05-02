# tests/test_strategy.py
"""
Tests for the BaseStrategy class.
"""

import asyncio
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from tradedesk.marketdata.subscriptions import MarketSubscription
from tradedesk.strategy.base import BaseStrategy


class TestBaseStrategy:
    """Test the BaseStrategy base class."""

    def test_initialization(self):
        """Test strategy initialization."""
        mock_client = MagicMock()

        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [
                MarketSubscription("CS.D.EURUSD.CFD.IP"),
                MarketSubscription("CS.D.GBPUSD.CFD.IP"),
            ]

            async def on_price_update(self, market_databid, offer, timestamp, raw_data):
                pass

        strategy = TestStrategy(mock_client)

        assert strategy._data_provider is mock_client
        assert strategy.watchdog_threshold == 60
        assert isinstance(strategy.last_update, datetime)

    def test_client_alias_emits_deprecation_warning(self):
        """`self.client` still resolves to the data provider but emits DeprecationWarning."""
        mock_client = MagicMock()

        class TestStrategy(BaseStrategy):
            SUBSCRIPTIONS = [MarketSubscription("CS.D.EURUSD.CFD.IP")]

            async def on_price_update(self, market_data):
                pass

        strategy = TestStrategy(mock_client)

        with pytest.warns(DeprecationWarning, match="self._data_provider"):
            assert strategy.client is mock_client


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

            async def on_price_update(self, market_data):
                self.updates.append(
                    {
                        "instrument": market_data.instrument,
                        "bid": market_data.bid,
                        "offer": market_data.offer,
                    }
                )

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
