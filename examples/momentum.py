"""Simple momentum strategy example."""
from typing import Any, Dict
from collections import deque
import logging

from tradedesk import BaseStrategy, IGClient, run_strategies

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
    EPICS = ["CS.D.GBPUSD.TODAY.IP", "CS.D.EURUSD.TODAY.IP"]
    
    def __init__(self, client: IGClient, lookback: int = 10):
        super().__init__(client)
        self.lookback = lookback
        
        # Track price history per EPIC
        self.price_history: Dict[str, deque] = {
            epic: deque(maxlen=lookback) for epic in self.EPICS
        }
    
    async def on_price_update(
        self,
        epic: str,
        bid: float,
        offer: float,
        timestamp: str,
        raw_data: Dict[str, Any]
    ) -> None:
        """Process price update and check for signals."""

        mid = (bid + offer) / 2

        # Store price
        self.price_history[epic].append(mid)
        
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
            # self.client.place_market_order(epic, "BUY", size=1.0)
        elif momentum < -0.001:  # 10 pips down
            log.info("🔴 %s momentum DOWN: %.5f", epic, momentum)
            # self.client.place_market_order(epic, "SELL", size=1.0)

if __name__ == "__main__":    
    run_strategies([
        MomentumStrategy,
    ])
