# examples/momentum_strategy.py
"""
Simple momentum strategy example.

Tracks short-term price momentum across two FX instruments and logs signals.
Order execution is commented out — uncomment to place real trades.

Usage (live):
  IG_API_KEY=... IG_USERNAME=... IG_PASSWORD=... IG_ENVIRONMENT=DEMO \
    python ./momentum_strategy.py

Usage (backtest from CSV):
  python ./momentum_strategy.py --backtest
"""

import logging
from collections import deque
from pathlib import Path

from tradedesk import SimplePortfolio, run_portfolio
from tradedesk.execution.ig import IGClient
from tradedesk.marketdata import MarketData, MarketSubscription
from tradedesk.strategy import BaseStrategy

log = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    """
    Tracks price momentum and generates signals.

    This is a simplified example — real strategies would include:
    - Proper position sizing
    - Risk management
    - Stop losses
    - Order execution
    """

    SUBSCRIPTIONS = [
        MarketSubscription("CS.D.GBPUSD.TODAY.IP"),
        MarketSubscription("CS.D.EURUSD.TODAY.IP"),
    ]

    def __init__(self, client, lookback: int = 10):
        super().__init__(client)
        self.lookback = lookback
        instruments = [sub.instrument for sub in self.SUBSCRIPTIONS]
        self.price_history: dict[str, deque] = {
            instrument: deque(maxlen=lookback) for instrument in instruments
        }

    async def on_price_update(self, market_data: MarketData) -> None:
        """Process price update and check for momentum signals."""
        mid = (market_data.bid + market_data.offer) / 2
        instrument = market_data.instrument

        if instrument not in self.price_history:
            return

        self.price_history[instrument].append(mid)

        if len(self.price_history[instrument]) < self.lookback:
            return

        prices = list(self.price_history[instrument])
        momentum = (prices[-1] - prices[0]) / prices[0]
        log.debug("Momentum %s: %.5f", instrument, momentum)

        if momentum > 0.001:
            log.info("UP signal: %s momentum=%.5f", instrument, momentum)
            # await self.client.place_market_order(instrument, "BUY", size=1.0)
        elif momentum < -0.001:
            log.info("DOWN signal: %s momentum=%.5f", instrument, momentum)
            # await self.client.place_market_order(instrument, "SELL", size=1.0)


if __name__ == "__main__":
    import sys

    if "--backtest" in sys.argv:
        from tradedesk.execution.backtest.client import BacktestClient

        def client_factory():
            return BacktestClient.from_market_csvs(
                {
                    "CS.D.GBPUSD.TODAY.IP": Path("gbpusd_ticks.csv"),
                    "CS.D.EURUSD.TODAY.IP": Path("eurusd_ticks.csv"),
                }
            )
    else:
        client_factory = IGClient

    run_portfolio(
        portfolio_factory=lambda c: SimplePortfolio(c, MomentumStrategy(c)),
        client_factory=client_factory,
        log_level="DEBUG",
    )
