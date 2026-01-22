# tradedesk/strategy.py
"""
Base strategy framework for trading strategies.

Provides infrastructure for:
- Lightstreamer streaming (real-time price feeds)
- REST polling fallback (for testing/backup)
- Multi-instrument subscription management

Strategies implement trading logic by subclassing BaseStrategy and
overriding on_price_update() and/or on_candle_update().
"""

import abc
import asyncio
import logging
from datetime import datetime, timezone
from tradedesk.subscriptions import MarketSubscription, ChartSubscription
from tradedesk.marketdata import Candle, ChartHistory, MarketData
from tradedesk.indicators.base import Indicator
from tradedesk.providers import Client
from tradedesk.marketdata import CandleClose


log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Abstract base class for all strategies.
# ----------------------------------------------------------------------
class BaseStrategy(abc.ABC):
    """
    Base class for all trading strategies.

    Provides common infrastructure for market data streaming and processing.
    Subclasses implement trading logic by overriding on_price_update() and/or
    on_candle_update().

    Example:
        class MyStrategy(BaseStrategy):
            SUBSCRIPTIONS = [
                MarketSubscription("CS.D.GBPUSD.TODAY.IP"),
                ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE"),
            ]

            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                # Handle tick-level updates
                pass

            async def on_candle_update(self, epic, period, candle):
                # Handle completed candles
                wr = self.wr.update(candle)
                if wr and wr < -80:
                    log.info("Oversold!")
    """

    # Subclasses should define which data streams they want
    SUBSCRIPTIONS: list[MarketSubscription | ChartSubscription] = []

    # Default polling interval when streamer is unavailable
    POLL_INTERVAL = 5  # seconds

    def __init__(
        self,
        client: Client,
        subscriptions: list[MarketSubscription | ChartSubscription] | None = None,
    ):
        """
        Initialize the strategy.

        Args:
            client: Authenticated provider client
            subscriptions: Optional explicit subscriptions for this instance.
                If omitted, defaults to the class-level SUBSCRIPTIONS.
            chart_history_length: Number of candles to retain per chart subscription.
        """
        self.client = client
        self.subscriptions = (
            list(subscriptions)
            if subscriptions is not None
            else list(self.SUBSCRIPTIONS)
        )

        # Create chart history managers for each chart subscription
        self.charts: dict[tuple[str, str], ChartHistory] = {}
        # ChartSubscription is not hashable, cannot use one to key the dict
        self._chart_indicators: dict[tuple[str, str], list[Indicator]] = {}

        for sub in self.subscriptions:
            if isinstance(sub, ChartSubscription):
                key = (sub.epic, sub.period)
                self.charts[key] = ChartHistory(
                    sub.epic, sub.period, 200
                )  # max_chart_history

        # Initialize the watchdog timestamp
        self.last_update = datetime.now(timezone.utc)
        self.watchdog_threshold = 60  # seconds

        if not self.subscriptions:
            log.warning(
                "%s has no subscriptions defined. Set SUBSCRIPTIONS or set subscriptions in __init__.",
                self.__class__.__name__,
            )

    def _chart_key(self, sub: ChartSubscription) -> tuple[str, str]:
        return (sub.epic, sub.period)

    def register_indicator(self, sub: ChartSubscription, indicator: Indicator) -> None:
        """
        Register an indicator against a specific chart subscription.

        This is used to compute required warmup candle counts and (later) to support
        priming indicators with historical candles.
        """
        key = self._chart_key(sub)
        self._chart_indicators.setdefault(key, []).append(indicator)

    def warmup_enabled(self) -> bool:
        return True

    async def warmup(self) -> None:
        """
        Provider-neutral warmup entrypoint.

        Strategies may override this to implement custom warmup behaviour.
        """
        await self.warmup_from_provider()

    async def warmup_from_provider(self) -> None:
        """
        Fetch historical candles (if supported by the provider client) to warm up
        chart history and indicators.

        Providers should implement `get_historical_candles(epic, period, num_points)`.
        If the client does not support history, warmup is skipped.
        """
        if not self.warmup_enabled():
            return

        plan = self.chart_warmup_plan()
        log.debug("Warmup plan: %s", plan)

        if not any(w > 0 for w in plan.values()):
            log.debug("No warmup required (no indicators registered)")
            return

        get_hist = getattr(self.client, "get_historical_candles", None)
        if not callable(get_hist):
            log.debug("Client does not support historical candles; skipping warmup")
            return

        history: dict[tuple[str, str], list[Candle]] = {}

        for (epic, period), warmup in plan.items():
            if warmup <= 0:
                continue
            try:
                candles = await get_hist(epic, period, warmup)
                log.debug(
                    "Warmup fetched %d candles for %s %s",
                    len(candles or []),
                    epic,
                    period,
                )
                history[(epic, period)] = candles or []
            except Exception:
                log.exception(
                    "Warmup fetch failed for %s %s; continuing without warmup",
                    epic,
                    period,
                )

        self.warmup_from_history(history)

    def warmup_from_history(self, history: dict[tuple[str, str], list[Candle]]) -> None:
        """
        Warm up chart histories and registered indicators from supplied historical candles.

        Args:
            history: Dict keyed by (epic, period) with candles ordered oldest -> newest.

        Notes:
            - Only chart subscriptions in chart_warmup_plan() are considered.
            - Missing history entries are skipped silently.
            - Extra history entries not present in subscriptions are ignored.
            - This does NOT call on_candle_update().
        """
        for epic_period, _warmup in self.chart_warmup_plan().items():
            candles = history.get(epic_period)
            if not candles:
                continue

            epic, period = epic_period
            self.prime_chart(ChartSubscription(epic, period), candles)

    def chart_warmup_plan(self) -> dict[tuple[str, str], int]:
        """
        Build a warmup plan for chart subscriptions.

        Returns:
            A dict keyed by (epic, period) with the number of completed candles
            required to warm up all registered indicators for that chart.
        """
        plan: dict[tuple[str, str], int] = {}

        for sub in self.subscriptions:
            if not isinstance(sub, ChartSubscription):
                continue

            key = (sub.epic, sub.period)
            plan[key] = self.required_warmup(sub)

        return plan

    def required_warmup(self, sub: ChartSubscription) -> int:
        """
        Return the number of completed candles required to warm up all registered
        indicators for the given chart subscription.
        """
        key = self._chart_key(sub)
        indicators = self._chart_indicators.get(key, [])
        return max((ind.warmup_periods() for ind in indicators), default=0)

    def prime_chart(self, sub: ChartSubscription, candles: list[Candle]) -> None:
        """
        Prime chart history and registered indicators with historical candles.

        Notes:
        - Candles are assumed to be ordered oldest -> newest.
        - This does NOT call on_candle_update(), so strategy trading logic is not triggered.
        """
        key = (sub.epic, sub.period)

        chart = self.charts.get(key)
        indicators = self._chart_indicators.get(key, [])

        for candle in candles:
            if chart is not None:
                chart.add_candle(candle)

            for ind in indicators:
                ind.update(candle)

    def _has_streamer(self) -> bool:
        get_streamer = getattr(self.client, "get_streamer", None)
        return callable(get_streamer)

    async def on_price_update(self, market_data: MarketData) -> None:
        """
        Called whenever a price update is received for a subscribed MARKET.

        This is where you implement tick-level trading logic for market subscriptions.
        """
        pass

    async def on_candle_close(self, candle_close: CandleClose) -> None:
        """
        Called when a candle completes for a subscribed CHART.

        Default implementation stores candle in chart history.
        Override to implement your candle-based trading logic.
        """
        # Store in chart history by default
        key = (candle_close.epic, candle_close.period)
        if key in self.charts:
            self.charts[key].add_candle(candle_close.candle)

    async def run(self) -> None:
        """
        Start the strategy; runs until cancelled.

        Note: This method is typically called by the runner, not directly.
        The runner orchestrates multiple strategies with a shared connection.
        """
        # Build display string for subscriptions
        sub_display = []
        for sub in self.subscriptions:
            if isinstance(sub, MarketSubscription):
                sub_display.append(f"MARKET:{sub.epic}")
            elif isinstance(sub, ChartSubscription):
                sub_display.append(f"CHART:{sub.epic}:{sub.period}")

        log.info("%s started for %s", self.__class__.__name__, ", ".join(sub_display))

        try:
            await self.warmup()
        except Exception:
            log.exception("Warmup failed; continuing without warmup")

        # Check if Lightstreamer is available
        if self._has_streamer():
            await self._run_streaming()
        else:
            log.info("Falling back to polling mode (Lightstreamer not available)")
            await self._run_polling()

    async def _run_polling(self) -> None:
        """
        Fallback polling mode - fetches market snapshots at regular intervals.
        Used when Lightstreamer is unavailable (typically in tests).

        Note: Only polls MARKET subscriptions, not CHART subscriptions.
        """
        # Only poll market subscriptions
        market_epics = [
            sub.epic
            for sub in self.subscriptions
            if isinstance(sub, MarketSubscription)
        ]

        if not market_epics:
            log.warning("No market subscriptions to poll")
            await asyncio.Future()  # Wait forever
            return

        last_prices: dict[str, float | None] = {epic: None for epic in market_epics}

        while True:
            for epic in market_epics:
                try:
                    snapshot = await self.client.get_market_snapshot(epic)
                    bid = float(snapshot["snapshot"]["bid"])
                    offer = float(snapshot["snapshot"]["offer"])
                    mid = (bid + offer) / 2

                    # Only notify on price changes
                    if last_prices[epic] != mid:
                        last_prices[epic] = mid
                        timestamp = (
                            datetime.now(timezone.utc).isoformat(timespec="seconds")
                            + "Z"
                        )
                        market_data = MarketData(
                            epic=epic,
                            bid=bid,
                            offer=offer,
                            timestamp=timestamp,
                            raw=snapshot,
                        )
                        await self.on_price_update(market_data)

                except Exception:
                    log.exception("Failed to fetch market snapshot for %s", epic)

            await asyncio.sleep(self.POLL_INTERVAL)

    async def _run_streaming(self) -> None:
        streamer = self.client.get_streamer()
        await streamer.run(self)

    async def _handle_event(self, event: MarketData | CandleClose) -> None:
        """
        Internal event dispatcher.

        Streamer implementations should call this method only. It updates common
        bookkeeping (e.g. last_update) and dispatches to the existing strategy
        callbacks to preserve the current public strategy API.
        """
        self.last_update = datetime.now(timezone.utc)

        if isinstance(event, MarketData):
            await self.on_price_update(event)

        elif isinstance(event, CandleClose):
            await self.on_candle_close(event)

        else:
            # Defensive: should never happen unless someone extends events incorrectly.
            raise TypeError(f"Unsupported event type: {type(event)!r}")
