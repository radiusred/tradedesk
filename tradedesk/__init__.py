# tradedesk/__init__.py
"""
Tradedesk - Trading infrastructure library for IG Markets.
Copyright 2026 Radius Red Ltd.

Provides authenticated API access, Lightstreamer streaming, and a base
framework for implementing trading strategies.

Quick start:
    1. Create a .env file with your IG credentials
    2. Implement your strategy by subclassing BaseStrategy
    3. Call run_strategies() with your strategy classes

Example:
    # my_strategies.py
    from tradedesk import BaseStrategy
    
    class MyStrategy(BaseStrategy):
        SUBSCRIPTIONS = [MarketSubscription("CS.D.GBPUSD.TODAY.IP")]
        
        async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
            # Your trading logic here
            pass
    
    # main.py
    import asyncio
    from tradedesk.providers.ig.client import IGClient
    from tradedesk import run_strategies
    from my_strategies import MyStrategy
    
    if __name__ == "__main__":
        client = IGClient()
        await client.start()
        run_strategies(client, [MyStrategy])
"""

from .strategy import BaseStrategy
from .runner import run_strategies
from .subscriptions import MarketSubscription, ChartSubscription
from .marketdata import MarketData, Candle, CandleClose, ChartHistory

__all__ = [
    "BaseStrategy", 
    "run_strategies",
    "MarketData",
    "MarketSubscription",
    "ChartSubscription",
    "Candle",
    "CandleClose",
    "ChartHistory",
]
