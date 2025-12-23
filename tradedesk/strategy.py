# tradedesk/strategy.py
"""
Base strategy framework for trading strategies.

Provides infrastructure for:
- Lightstreamer streaming (real-time price feeds)
- REST polling fallback (for testing/backup)
- Multi-instrument subscription management

Strategies implement trading logic by subclassing BaseStrategy and
overriding on_price_update().
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

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Abstract base class for all strategies.
# ----------------------------------------------------------------------
class BaseStrategy(abc.ABC):
    """
    Base class for all trading strategies.
    
    Provides common infrastructure for market data streaming and processing.
    Subclasses implement trading logic by overriding on_price_update().
    
    Example:
        class MyStrategy(BaseStrategy):
            EPICS = ["CS.D.GBPUSD.TODAY.IP", "CS.D.EURUSD.TODAY.IP"]
            
            async def on_price_update(self, epic, bid, offer, timestamp, raw_data):
                # Your trading logic here
                mid = (bid + offer) / 2
                print(f"{epic}: {mid}")
    """
    
    # Subclasses should define which EPICs they want to monitor
    # This will be used by the runner to create subscriptions
    EPICS: list[str] = []
    
    # Default polling interval when Lightstreamer is unavailable
    POLL_INTERVAL = 2  # seconds
    
    def __init__(self, client: IGClient):
        """
        Initialize the strategy.
        
        Args:
            client: Authenticated IG client
        """
        self.client = client
        self.epics = self.EPICS if self.EPICS else []
        # Initialize the watchdog timestamp
        self.last_update = datetime.now(timezone.utc)
        self.watchdog_threshold = 60  # seconds
        
        if not self.epics:
            log.warning(
                "%s has no EPICs defined. Set the EPICS class attribute "
                "to specify which instruments to monitor.",
                self.__class__.__name__
            )
    
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
        Called whenever a price update is received for a subscribed EPIC.
        
        This is where you implement your trading logic:
        - Generate trading signals
        - Manage positions
        - Calculate indicators
        - Place orders via self.client.place_market_order()
        
        Args:
            epic: The instrument EPIC code
            bid: Current bid price
            offer: Current offer price  
            timestamp: ISO 8601 timestamp of the update
            raw_data: Full raw data from the price feed (varies by mode)
        """
        ...
    
    async def run(self) -> None:
        """
        Start the strategy; runs until cancelled.
        
        Note: This method is typically called by the runner, not directly.
        The runner orchestrates multiple strategies with a shared connection.
        """
        log.info("%s started for %s", self.__class__.__name__, ", ".join(self.epics))
        
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
        """
        last_prices = {epic: None for epic in self.epics}
        
        while True:
            for epic in self.epics:
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
        """
        if LightstreamerClient is None or Subscription is None:
            raise RuntimeError("Lightstreamer client library not available")
        
        log.info("Starting Lightstreamer streaming for %s", ", ".join(self.epics))
        
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        
        # --- GET THE CURRENT LOOP ---
        loop = asyncio.get_running_loop()

        # Create Lightstreamer client
        ls_client = LightstreamerClient(self.client.ls_url, "DEFAULT")
        ls_client.connectionDetails.setUser(self.client.client_id or self.client.account_id or "")
        ls_client.connectionDetails.setPassword(
            f"CST-{self.client.ls_cst}|XST-{self.client.ls_xst}"
        )
        
        log.info("LS connecting to %s with clientId %s", 
                 self.client.ls_url, self.client.client_id)
        
        # Subscribe to all EPICs
        item_names = [f"MARKET:{epic}" for epic in self.epics]
        sub = Subscription(
            mode="MERGE",
            items=item_names,
            fields=["UPDATE_TIME", "BID", "OFFER", "MARKET_STATE"]
        )
        
        # Create listener that routes updates to the queue
        epics = self.epics  # Capture for closure
        
        class PriceListener:
            def onItemUpdate(self, update):
                """Callback invoked by Lightstreamer for each price push."""
                try:
                    bid_str = update.getValue("BID")
                    offer_str = update.getValue("OFFER")
                    item_name = update.getItemName()
                    
                    if not bid_str or not offer_str:
                        log.debug("Received update with missing BID/OFFER fields")
                        return
                    
                    # Extract EPIC from item name (MARKET:epic format)
                    epic = item_name.split(":", 1)[1] if ":" in item_name else item_name
                    
                    log.debug("LS update for %s: BID=%s, OFFER=%s", epic, bid_str, offer_str)
                    
                    data = {
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
                    
                    loop.call_soon_threadsafe(queue.put_nowait, data)

                except Exception as e:
                    log.exception("Error processing Lightstreamer update: %s", e)
            
            def onSubscriptionError(self, code, message):
                log.error("Lightstreamer subscription error: %s - %s", code, message)
            
            def onSubscription(self):
                log.info("Lightstreamer subscription active for %s", ", ".join(epics))
            
            def onUnsubscription(self):
                log.info("Lightstreamer unsubscribed from %s", ", ".join(epics))
        
        listener = PriceListener()
        sub.addListener(listener)
        
        # Add connection status listener
        class ConnectionListener:
            def onStatusChange(self, status):
                log.info("Lightstreamer connection status: %s", status)
            
            def onServerError(self, code, message):
                log.error("Lightstreamer server error: %s - %s", code, message)
        
        ls_client.addListener(ConnectionListener())
        
        ls_client.connect()
        ls_client.subscribe(sub)
        
        log.info("Lightstreamer subscribed to %s", ", ".join(item_names))
        
        async def _heartbeat_monitor():
            """Check if the stream has gone silent."""
            while True:
                await asyncio.sleep(10) # Check every 10s
                delta = (datetime.now(timezone.utc) - self.last_update).total_seconds()
                
                if delta > self.watchdog_threshold:
                    log.warning(
                        "⚠️ HEARTBEAT ALERT: No updates for %s in %.1fs. "
                        "Connection may be stale.", 
                        ", ".join(self.epics), delta
                    )
                else:
                    log.debug("Heartbeat OK: Last update %.1fs ago", delta)

        # Consumer task - dispatches updates to strategy
        async def consumer():
            while True:
                payload = await queue.get()
                # Update the watchdog timestamp
                self.last_update = datetime.now(timezone.utc)
                
                await self.on_price_update(
                    epic=payload["epic"],
                    bid=payload["bid"],
                    offer=payload["offer"],
                    timestamp=payload["timestamp"],
                    raw_data=payload["raw"]
                )
        
        consumer_task = asyncio.create_task(consumer())
        monitor_task = asyncio.create_task(_heartbeat_monitor())
        
        try:
            await asyncio.Future() 

        except asyncio.CancelledError:
            log.info("%s cancelled – cleaning up Lightstreamer", self.__class__.__name__)

        finally:
            consumer_task.cancel()
            monitor_task.cancel()
            ls_client.disconnect()
