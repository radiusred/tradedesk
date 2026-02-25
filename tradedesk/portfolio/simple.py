"""Simple single-strategy portfolio with no risk allocation."""

from tradedesk.execution import Client
from tradedesk.marketdata import CandleClosedEvent, MarketData
from tradedesk.strategy import BaseStrategy

from .base import BasePortfolio


class SimplePortfolio(BasePortfolio):
    """Portfolio that wraps a single strategy with no risk allocation.

    The strategy handles its own warmup via SessionStartedEvent (subscribed in
    BaseStrategy.__init__). The portfolio's sole job is to forward candle events.

    Example::

        def portfolio_factory(client):
            strategy = MyStrategy(client)
            return SimplePortfolio(client, strategy)

        run_portfolio(portfolio_factory, client_factory)
    """

    def __init__(self, client: Client, strategy: BaseStrategy) -> None:
        super().__init__(client)
        self._strategy = strategy
        self.subscriptions = strategy.subscriptions

    async def on_price_update(self, data: MarketData) -> None:
        await self._strategy.on_price_update(data)

    async def on_candle_close(self, event: CandleClosedEvent) -> None:
        await self._strategy.on_candle_close(event)
