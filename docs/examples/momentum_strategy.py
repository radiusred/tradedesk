# examples/momentum.py
"""
Simple momentum strategy example.

Usage:
  IG_API_KEY=... IG_USERNAME=... IG_PASSWORD=... IG_ENVIRONMENT=DEMO \
    python ./momentum_strategy.py
    
"""
import logging
from collections import deque

from tradedesk import run_strategies
from tradedesk.execution import Client
from tradedesk.execution.ig import IGClient
from tradedesk.marketdata import MarketData, MarketSubscription
from tradedesk.strategy import BaseStrategy

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

        # Track price history per instrument
        instruments = [sub.instrument for sub in self.SUBSCRIPTIONS]
        self.price_history: dict[str, deque] = {
            instrument: deque(maxlen=lookback) for instrument in instruments
        }
    
    async def on_price_update(self, market_data: MarketData) -> None:
        """Process price update and check for signals."""

        mid = (market_data.bid + market_data.offer) / 2
        instrument = market_data.instrument

        # Store price
        if instrument in self.price_history:
            self.price_history[instrument].append(mid)
        else:
            # Instrument not in our list, ignore
            return

        # Need full history for momentum calculation
        if len(self.price_history[instrument]) < self.lookback:
            return

        # Calculate simple momentum
        prices = list(self.price_history[instrument])
        momentum = (prices[-1] - prices[0]) / prices[0]
        log.debug(
            "Momentum for %s: %.5f (from %.5f to %.5f)",
            instrument, momentum, prices[0], prices[-1]
        )

        # Generate signals (in production, would place actual orders)
        if momentum > 0.001:  # 10 pips up
            log.info("🟢 %s momentum UP: %.5f", instrument, momentum)
            # await self.client.place_market_order(instrument, "BUY", size=1.0)
        elif momentum < -0.001:  # 10 pips down
            log.info("🔴 %s momentum DOWN: %.5f", instrument, momentum)
            # await self.client.place_market_order(instrument, "SELL", size=1.0)

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
