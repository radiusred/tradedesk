# examples/momentum.py
"""Simple momentum strategy example."""
from typing import Any
from collections import deque
import logging

from tradedesk import BaseStrategy, run_strategies
from tradedesk.marketdata import MarketData
from tradedesk.providers.backtest.client import BacktestClient
from tradedesk.providers.base import Client
from tradedesk.subscriptions import MarketSubscription
from tradedesk.providers.ig.client import IGClient

log = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    """
    Tracks price momentum and generates signals.
    
    This is a simplified example - real strategies would include:
    - Proper position sizing
    - Risk management
    - Stop losses
    - Order execution
    
    """
    
    # Declare which instruments to monitor
    SUBSCRIPTIONS = [
        MarketSubscription("CS.D.GBPUSD.TODAY.IP"),
        MarketSubscription("CS.D.EURUSD.TODAY.IP"),
    ]

    def __init__(self, client: Client, config: dict = None, lookback: int = 10):
        """
        Initialize momentum strategy.
        
        Args:
            client: Authenticated IG client or Backtest client
            config: Optional configuration dict
            lookback: Number of price updates to track for momentum
        """
        super().__init__(client, config)
        self.lookback = lookback
        
        # Track price history per EPIC
        epics = [sub.epic for sub in self.SUBSCRIPTIONS]
        self.price_history: dict[str, deque] = {
            epic: deque(maxlen=lookback) for epic in epics
        }
    
    async def on_price_update(self, market_data: MarketData) -> None:
        """Process price update and check for signals."""

        mid = (market_data.bid + market_data.offer) / 2
        epic = market_data.epic
        
        # Store price
        if epic in self.price_history:
            self.price_history[epic].append(mid)
        else:
            # Epic not in our list, ignore
            return
        
        # Need full history for momentum calculation
        if len(self.price_history[epic]) < self.lookback:
            return
        
        # Calculate simple momentum
        prices = list(self.price_history[epic])
        momentum = (prices[-1] - prices[0]) / prices[0]
        log.debug(
            "Momentum for %s: %.5f (from %.5f to %.5f)",
            epic, momentum, prices[0], prices[-1]
        )

        # Generate signals (in production, would place actual orders)
        if momentum > 0.001:  # 10 pips up
            log.info("🟢 %s momentum UP: %.5f", epic, momentum)
            # await self.client.place_market_order(epic, "BUY", size=1.0)
        elif momentum < -0.001:  # 10 pips down
            log.info("🔴 %s momentum DOWN: %.5f", epic, momentum)
            # await self.client.place_market_order(epic, "SELL", size=1.0)

if __name__ == "__main__":
    # choose which client to run the strategy via..

    cf = IGClient
    # cf = lambda: BacktestClient.from_market_csvs({
    #         "CS.D.GBPUSD.TODAY.IP": "gbpusd_ticks.csv",
    #         "CS.D.EURUSD.TODAY.IP": "eurusd_ticks.csv",
    #     })
    
    run_strategies(
        strategy_specs=[MomentumStrategy],
        client_factory=cf,
        log_level="DEBUG",
    )
