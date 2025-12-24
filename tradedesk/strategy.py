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

# ----------------------------------------------------------------------
# Lightstreamer import – optional for the production daemon.
# ----------------------------------------------------------------------
try:
    from lightstreamer.client import LightstreamerClient, Subscription
except Exception:  # pragma: no cover – only triggered in the test env
    LightstreamerClient = None   # type: ignore
    Subscription = None           # type: ignore

from .client import IGClient
from .config import settings
from .subscriptions import MarketSubscription, ChartSubscription
from .chartdata import Candle, ChartHistory

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
    
    # Legacy support - converts to MarketSubscription
    EPICS: list[str] = []
    
    # Default polling interval when Lightstreamer is unavailable
    POLL_INTERVAL = 2  # seconds
    
    def __init__(self, client: IGClient, config: dict = None):
        """
        Initialize the strategy.
        
        Args:
            client: Authenticated IG client
            config: Optional configuration dictionary (e.g., from YAML)
        """
        self.client = client
        self.config = config or {}
        
        # Build subscription list FIRST before using it
        self._build_subscriptions()
        
        # Create chart history managers for each chart subscription
        self.charts: dict[tuple[str, str], ChartHistory] = {}
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
                "%s has no subscriptions defined. Set SUBSCRIPTIONS or EPICS "
                "to specify which instruments to monitor.",
                self.__class__.__name__
            )
    
    def _build_subscriptions(self) -> None:
        """Build subscription list from SUBSCRIPTIONS and legacy EPICS."""
        self.subscriptions = list(self.SUBSCRIPTIONS)
        
        # Legacy support: convert EPICS to MarketSubscription
        if self.EPICS and not self.SUBSCRIPTIONS:
            log.debug(
                "%s uses legacy EPICS attribute. Consider migrating to SUBSCRIPTIONS.",
                self.__class__.__name__
            )
            self.subscriptions = [MarketSubscription(epic) for epic in self.EPICS]
        
        # Maintain legacy epics property for backward compatibility
        self.epics = [
            sub.epic for sub in self.subscriptions
            if isinstance(sub, MarketSubscription)
        ]
    
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
        
        # Check if Lightstreamer is available
        if self._is_lightstreamer_available():
            await self._run_streaming()
        else:
            log.info("Falling back to polling mode (Lightstreamer not available)")
            await self._run_polling()
    
    def _is_lightstreamer_available(self) -> bool:
        """Check if all requirements for Lightstreamer are met."""
        return (
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
        """
        Production streaming mode - connects to Lightstreamer for real-time updates.
        Handles both MARKET and CHART subscriptions.
        """
        if LightstreamerClient is None or Subscription is None:
            raise RuntimeError("Lightstreamer client library not available")
        
        log.info("Starting Lightstreamer streaming for %s subscriptions", len(self.subscriptions))
        
        # Create separate queues for different data types
        market_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        chart_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        
        # Get the current loop
        loop = asyncio.get_running_loop()

        # Create Lightstreamer client
        ls_client = LightstreamerClient(self.client.ls_url, "DEFAULT")
        ls_client.connectionDetails.setUser(self.client.client_id or self.client.account_id or "")
        ls_client.connectionDetails.setPassword(
            f"CST-{self.client.ls_cst}|XST-{self.client.ls_xst}"
        )
        
        log.info("LS connecting to %s with clientId %s", 
                 self.client.ls_url, self.client.client_id)
        
        # Set up subscriptions by type
        market_subs = [s for s in self.subscriptions if isinstance(s, MarketSubscription)]
        chart_subs = [s for s in self.subscriptions if isinstance(s, ChartSubscription)]
        
        subscriptions = []
        
        # Handle market subscriptions
        if market_subs:
            market_items = [sub.get_item_name() for sub in market_subs]
            market_sub = Subscription(
                mode="MERGE",
                items=market_items,
                fields=market_subs[0].get_fields()  # All market subs use same fields
            )
            
            class MarketListener:
                def onItemUpdate(self, update):
                    try:
                        bid_str = update.getValue("BID")
                        offer_str = update.getValue("OFFER")
                        
                        if not bid_str or not offer_str:
                            return
                        
                        item_name = update.getItemName()
                        epic = item_name.split(":", 1)[1] if ":" in item_name else item_name
                        
                        data = {
                            "type": "market",
                            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
                            "epic": epic,
                            "bid": float(bid_str),
                            "offer": float(offer_str),
                            "raw": {
                                "BID": bid_str,
                                "OFFER": offer_str,
                                "UPDATE_TIME": update.getValue("UPDATE_TIME"),
                                "MARKET_STATE": update.getValue("MARKET_STATE"),
                            },
                        }
                        
                        loop.call_soon_threadsafe(market_queue.put_nowait, data)
                    except Exception as e:
                        log.exception("Error processing market update: %s", e)
                
                def onSubscriptionError(self, code, message):
                    log.error("Market subscription error: %s - %s", code, message)
                
                def onSubscription(self):
                    log.info("Market subscription active")
                
                def onUnsubscription(self):
                    log.info("Market unsubscribed")
            
            market_sub.addListener(MarketListener())
            subscriptions.append(market_sub)
        
        # Handle chart subscriptions
        if chart_subs:
            for chart_sub in chart_subs:
                item_name = chart_sub.get_item_name()
                ls_sub = Subscription(
                    mode="MERGE",
                    items=[item_name],
                    fields=chart_sub.get_fields()
                )
                
                # Closure to capture chart_sub for this specific subscription
                def make_chart_listener(sub):
                    class ChartListener:
                        def onItemUpdate(self, update):
                            try:
                                # Check if candle is complete
                                cons_end = update.getValue("CONS_END")
                                if cons_end != "1":
                                    return  # Candle not complete yet
                                
                                # Extract OHLCV data
                                # Use mid price from bid/offer
                                ofr_open = update.getValue("OFR_OPEN")
                                ofr_high = update.getValue("OFR_HIGH")
                                ofr_low = update.getValue("OFR_LOW")
                                ofr_close = update.getValue("OFR_CLOSE")
                                
                                bid_open = update.getValue("BID_OPEN")
                                bid_high = update.getValue("BID_HIGH")
                                bid_low = update.getValue("BID_LOW")
                                bid_close = update.getValue("BID_CLOSE")
                                
                                if not all([ofr_close, bid_close]):
                                    return
                                
                                # Calculate mid prices
                                open_price = (float(ofr_open or ofr_close) + float(bid_open or bid_close)) / 2
                                high_price = (float(ofr_high or ofr_close) + float(bid_high or bid_close)) / 2
                                low_price = (float(ofr_low or ofr_close) + float(bid_low or bid_close)) / 2
                                close_price = (float(ofr_close) + float(bid_close)) / 2
                                
                                # Extract volume
                                ltv = update.getValue("LTV")
                                tick_count = update.getValue("CONS_TICK_COUNT")
                                
                                volume = float(ltv) if ltv else 0.0
                                ticks = int(tick_count) if tick_count else 0
                                
                                data = {
                                    "type": "chart",
                                    "epic": sub.epic,
                                    "period": sub.period,
                                    "candle": {
                                        "timestamp": update.getValue("UTM") or datetime.now(timezone.utc).isoformat(),
                                        "open": open_price,
                                        "high": high_price,
                                        "low": low_price,
                                        "close": close_price,
                                        "volume": volume,
                                        "tick_count": ticks,
                                    }
                                }
                                
                                loop.call_soon_threadsafe(chart_queue.put_nowait, data)
                            except Exception as e:
                                log.exception("Error processing chart update: %s", e)
                        
                        def onSubscriptionError(self, code, message):
                            log.error("Chart subscription error for %s: %s - %s", sub.epic, code, message)
                        
                        def onSubscription(self):
                            log.info("Chart subscription active for %s %s", sub.epic, sub.period)
                        
                        def onUnsubscription(self):
                            log.info("Chart unsubscribed for %s %s", sub.epic, sub.period)
                    
                    return ChartListener()
                
                ls_sub.addListener(make_chart_listener(chart_sub))
                subscriptions.append(ls_sub)
        
        # Add connection status listener
        class ConnectionListener:
            def onStatusChange(self, status):
                log.info("Lightstreamer connection status: %s", status)
            
            def onServerError(self, code, message):
                log.error("Lightstreamer server error: %s - %s", code, message)
        
        ls_client.addListener(ConnectionListener())
        
        # Connect and subscribe
        ls_client.connect()
        for sub in subscriptions:
            ls_client.subscribe(sub)
        
        log.info("Lightstreamer subscriptions active")
        
        async def _heartbeat_monitor():
            """Check if the stream has gone silent."""
            while True:
                await asyncio.sleep(10)
                delta = (datetime.now(timezone.utc) - self.last_update).total_seconds()
                
                if delta > self.watchdog_threshold:
                    log.warning(
                        "⚠️ HEARTBEAT ALERT: No updates for %s in %.1fs. "
                        "Connection may be stale.", 
                        self.__class__.__name__, delta
                    )
                else:
                    log.debug("Heartbeat OK: Last update %.1fs ago", delta)

        # Consumer tasks
        async def market_consumer():
            while True:
                payload = await market_queue.get()
                self.last_update = datetime.now(timezone.utc)
                
                await self.on_price_update(
                    epic=payload["epic"],
                    bid=payload["bid"],
                    offer=payload["offer"],
                    timestamp=payload["timestamp"],
                    raw_data=payload["raw"]
                )
        
        async def chart_consumer():
            while True:
                payload = await chart_queue.get()
                self.last_update = datetime.now(timezone.utc)
                
                candle_data = payload["candle"]
                candle = Candle(**candle_data)
                
                await self.on_candle_update(
                    epic=payload["epic"],
                    period=payload["period"],
                    candle=candle
                )
        
        tasks = [asyncio.create_task(_heartbeat_monitor())]
        
        if market_subs:
            tasks.append(asyncio.create_task(market_consumer()))
        
        if chart_subs:
            tasks.append(asyncio.create_task(chart_consumer()))
        
        try:
            await asyncio.Future() 

        except asyncio.CancelledError:
            log.info("%s cancelled – cleaning up Lightstreamer", self.__class__.__name__)

        finally:
            for task in tasks:
                task.cancel()
            ls_client.disconnect()
