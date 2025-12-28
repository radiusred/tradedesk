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
from typing import Any
from .providers import Client
from .config import settings
from .subscriptions import MarketSubscription, ChartSubscription
from .chartdata import Candle, ChartHistory
from .indicators.base import Indicator

# ----------------------------------------------------------------------
# Lightstreamer import – optional for the production daemon.
# ----------------------------------------------------------------------
try:
    from lightstreamer.client import LightstreamerClient, Subscription
except Exception:  # pragma: no cover – only triggered in the test env
    LightstreamerClient = None   # type: ignore
    Subscription = None           # type: ignore

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
    
    # Default polling interval when Lightstreamer is unavailable
    POLL_INTERVAL = 5  # seconds
    
    # TODO: abstract provider
    def __init__(self, client: Client, config: dict = None):
        """
        Initialize the strategy.
        
        Args:
            client: Authenticated IG client
            config: Optional configuration dictionary (e.g., from YAML)
        """
        self.client = client
        self.config = config or {}
        self.subscriptions = list(self.SUBSCRIPTIONS)

        # Create chart history managers for each chart subscription
        self.charts: dict[tuple[str, str], ChartHistory] = {}
        # ChartSubscription is not hashable, cannot use one to key the dict
        self._chart_indicators: dict[tuple[str, str], list[Indicator]] = {}

        for sub in self.subscriptions:
            if isinstance(sub, ChartSubscription):
                key = (sub.epic, sub.period)
                max_len = self.config.get('chart', {}).get('history_length', 200)
                self.charts[key] = ChartHistory(sub.epic, sub.period, max_len)
        
        # Initialize the watchdog timestamp
        self.last_update = datetime.now(timezone.utc)
        self.watchdog_threshold = 60  # seconds
        
        if not self.subscriptions:
            log.warning(
                "%s has no subscriptions defined. Set SUBSCRIPTIONS "
                "to specify which instruments to monitor.",
                self.__class__.__name__
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

    def required_warmup(self, sub: ChartSubscription) -> int:
        """
        Return the number of completed candles required to warm up all registered
        indicators for the given chart subscription.
        """
        key = self._chart_key(sub)
        indicators = self._chart_indicators.get(key, [])
        return max((ind.warmup_periods() for ind in indicators), default=0)
    
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

    # TODO: abstract provider
    async def warmup_from_ig(self) -> None:
        """
        Fetch historical candles from IG REST to warm up chart history and indicators.
        """
        enabled = self.config.get("warmup", {}).get("enabled", True)
        if not enabled:
            return

        history: dict[tuple[str, str], list[Candle]] = {}

        for (epic, period), warmup in self.chart_warmup_plan().items():
            if warmup <= 0:
                continue
            try:
                candles = await self.client.get_historical_candles(epic, period, warmup)
                history[(epic, period)] = candles or []
            except Exception:
                log.exception("Warmup fetch failed for %s %s; continuing without warmup", epic, period)

        self.warmup_from_history(history)

    @abc.abstractmethod
    async def on_price_update(
        self,
        epic: str,
        bid: float,
        offer: float,
        timestamp: str,
        raw_data: dict[str, Any]
    ) -> None:
        """
        Called whenever a price update is received for a subscribed MARKET.
        
        This is where you implement tick-level trading logic for market subscriptions.
        
        Args:
            epic: The instrument EPIC code
            bid: Current bid price
            offer: Current offer price  
            timestamp: ISO 8601 timestamp of the update
            raw_data: Full raw data from the price feed (varies by mode)
        """
        ...
    
    async def on_candle_update(
        self,
        epic: str,
        period: str,
        candle: Candle
    ) -> None:
        """
        Called when a candle completes for a subscribed CHART.
        
        Default implementation stores candle in chart history.
        Override to implement your candle-based trading logic.
        
        Args:
            epic: The instrument EPIC code
            period: Timeframe (e.g., "5MINUTE", "HOUR")
            candle: Completed candle with OHLCV data
        """
        # Store in chart history by default
        key = (epic, period)
        if key in self.charts:
            self.charts[key].add_candle(candle)
    
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
            await self.warmup_from_ig()
        except Exception:
            log.exception("Warmup failed; continuing without warmup")

        # Check if Lightstreamer is available
        if self._is_lightstreamer_available():
            await self._run_streaming()
        else:
            log.info("Falling back to polling mode (Lightstreamer not available)")
            await self._run_polling()
    
    def _is_lightstreamer_available(self) -> bool:
        """Check if all requirements for Lightstreamer are met."""
        return bool(
            LightstreamerClient is not None
            and isinstance(getattr(self.client, "ls_url", None), str)
            and getattr(self.client, "ls_url")
            and getattr(self.client, "ls_cst", None)
            and getattr(self.client, "ls_xst", None)
        )
    
    async def _run_polling(self) -> None:
        """
        Fallback polling mode - fetches market snapshots at regular intervals.
        Used when Lightstreamer is unavailable (typically in tests).
        
        Note: Only polls MARKET subscriptions, not CHART subscriptions.
        """
        # Only poll market subscriptions
        market_epics = [
            sub.epic for sub in self.subscriptions 
            if isinstance(sub, MarketSubscription)
        ]
        
        if not market_epics:
            log.warning("No market subscriptions to poll")
            await asyncio.Future()  # Wait forever
            return
        
        last_prices = {epic: None for epic in market_epics}
        
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
                        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z"
                        await self.on_price_update(epic, bid, offer, timestamp, snapshot)
                        
                except Exception:
                    log.exception("Failed to fetch market snapshot for %s", epic)
            
            await asyncio.sleep(self.POLL_INTERVAL)
    
    async def _run_streaming(self) -> None:
        streamer = self.client.get_streamer()
        await streamer.run(self)

