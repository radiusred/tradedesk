# examples/log_price_strategy.py
"""
Simple example strategy that logs price updates.

This demonstrates:
- How to subclass BaseStrategy
- How to declare which EPICs to monitor
- How to process price updates
- How to run the strategy live or against a backtest

Usage (live):
  IG_API_KEY=... IG_USERNAME=... IG_PASSWORD=... IG_ENVIRONMENT=DEMO \
  IG_ACCOUNT_ID=... \
    python ./log_price_strategy.py

Usage (backtest):
  python ./log_price_strategy.py --backtest
"""

import logging
import os

from tradedesk import SimplePortfolio, run_portfolio
from tradedesk.execution.ig.client import IGClient
from tradedesk.marketdata.instrument import MarketData
from tradedesk.marketdata.subscriptions import MarketSubscription
from tradedesk.strategy import BaseStrategy

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
    SUBSCRIPTIONS = [
        MarketSubscription(
            "CS.D.GBPUSD.TODAY.IP",
            account_id=os.getenv("IG_ACCOUNT_ID", ""),
        )
    ]

    def __init__(self, client):
        super().__init__(client)
        self._last_mid: float | None = None

    async def on_price_update(self, market_data: MarketData) -> None:
        """Log the price update. Only logs when the mid price changes."""
        mid = (market_data.bid + market_data.offer) / 2

        if self._last_mid is None or mid != self._last_mid:
            self._last_mid = mid
            log.info("price_update %s", market_data)


if __name__ == "__main__":
    run_portfolio(
        portfolio_factory=lambda c: SimplePortfolio(c, LogPriceStrategy(c)),
        client_factory=IGClient,
    )
