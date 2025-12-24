# tradedesk/__init__.py
"""
Tradedesk - Trading infrastructure library for IG Markets.

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
        EPICS = ["CS.D.GBPUSD.TODAY.IP"]
        
        async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
            # Your trading logic here
            pass
    
    # main.py
    from tradedesk import run_strategies
    from my_strategies import MyStrategy
    
    if __name__ == "__main__":
        run_strategies([MyStrategy])
"""

from .client import IGClient
from .strategy import BaseStrategy
from .runner import run_strategies
from .config import settings, load_strategy_config
from .subscriptions import MarketSubscription, ChartSubscription
from .chartdata import Candle, ChartHistory

__version__ = "0.1.0"
__all__ = [
    "IGClient",
    "BaseStrategy", 
    "run_strategies",
    "settings",
    "load_strategy_config",
    "MarketSubscription",
    "ChartSubscription",
    "Candle",
    "ChartHistory",
]
