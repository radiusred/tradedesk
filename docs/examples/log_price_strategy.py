# examples/log_price_strategy.py
"""
Simple example strategy that logs price updates.

This demonstrates:
- How to subclass BaseStrategy
- How to declare which EPICs to monitor
- How to process price updates
- How to run the strategy

Usage:
    python examples/log_price_strategy.py

Requirements:
    - .env file with IG credentials in the project root
    - tradedesk library installed
"""

import asyncio
import json
import logging
from typing import Any, Dict

from tradedesk import BaseStrategy, run_strategies
from tradedesk.marketdata import MarketData
from tradedesk.providers.ig.client import IGClient
from tradedesk.subscriptions import MarketSubscription

log = logging.getLogger(__name__)


class LogPriceStrategy(BaseStrategy):
    """
    Simple strategy that logs every price change.
    
    This is primarily used for:
    - Testing the infrastructure
    - Monitoring market activity  
    - Debugging connectivity issues
    - Learning how to build strategies
    
    Production strategies would implement actual trading logic in on_price_update()
    (signal generation, position management, risk management, etc.)
    """
    
    # Declare which EPICs this strategy wants to monitor
    SUBSCRIPTIONS = [MarketSubscription("CS.D.GBPUSD.TODAY.IP")]

    def __init__(self, client: IGClient):
        """
        Initialize the logging strategy.
        
        Args:
            client: Authenticated IG client
        """
        super().__init__(client)
        self._last_mid: float | None = None
    
    async def on_price_update(self, market_data: MarketData) -> None:
        """
        Log the price update to stdout as structured JSON.
        
        Only logs when the mid price changes to reduce noise.
        """
        mid = (market_data.bid + market_data.offer) / 2
        
        # Only log when mid price changes
        if self._last_mid is None or mid != self._last_mid:
            self._last_mid = mid
            
            # Log as structured JSON for easy parsing
            log.warning("price_update %s", json.dumps(market_data))

if __name__ == "__main__":
    run_strategies(
        strategy_specs=[LogPriceStrategy],
        client_factory=IGClient,
    )
