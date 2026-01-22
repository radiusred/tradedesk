import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from tradedesk.marketdata import Candle, MarketData
from tradedesk.subscriptions import MarketSubscription, ChartSubscription
from tradedesk.providers import Streamer
from tradedesk.marketdata import CandleClose

log = logging.getLogger(__name__)

# Optional import
try:
    from lightstreamer.client import LightstreamerClient, Subscription  # type: ignore[import-untyped]
except Exception:  # pragma: no cover
    LightstreamerClient = None
    Subscription = None


class Lightstreamer(Streamer):
    """
    IG Lightstreamer implementation of the provider-neutral Streamer interface.

    This class encapsulates all Lightstreamer-specific wiring and translates
    incoming updates into BaseStrategy callbacks.
    """

    def __init__(self, client: Any):
        self.client = client
        self._ls_client = None
        self.heartbeat_sleep = 10

    async def connect(self) -> None:
        # Connection is established inside run() to preserve the existing flow.
        return

    async def disconnect(self) -> None:
        if self._ls_client is not None:
            try:
                self._ls_client.disconnect()
            except Exception:
                log.exception("Lightstreamer disconnect failed")

    async def run(self, strategy: Any) -> None:
        if LightstreamerClient is None or Subscription is None:
            raise RuntimeError("Lightstreamer client library not available")

        log.info(
            "Starting Lightstreamer streaming for %s subscriptions",
            len(strategy.subscriptions),
        )

        market_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        chart_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        ls_client = LightstreamerClient(self.client.ls_url, "DEFAULT")
        self._ls_client = ls_client

        ls_client.connectionDetails.setUser(
            self.client.client_id or self.client.account_id or ""
        )
        ls_client.connectionDetails.setPassword(
            f"CST-{self.client.ls_cst}|XST-{self.client.ls_xst}"
        )

        log.info(
            "LS connecting to %s with clientId %s",
            self.client.ls_url,
            self.client.client_id,
        )

        market_subs = [
            s for s in strategy.subscriptions if isinstance(s, MarketSubscription)
        ]
        chart_subs = [
            s for s in strategy.subscriptions if isinstance(s, ChartSubscription)
        ]

        subscriptions = []

        if market_subs:
            market_items = [sub.get_item_name() for sub in market_subs]
            market_sub = Subscription(
                mode="MERGE",
                items=market_items,
                fields=market_subs[0].get_fields(),
            )

            class MarketListener:
                def onItemUpdate(self, update: Any) -> None:
                    try:
                        bid_str = update.getValue("BID")
                        offer_str = update.getValue("OFFER")

                        if not bid_str or not offer_str:
                            return

                        item_name = update.getItemName()
                        epic = (
                            item_name.split(":", 1)[1]
                            if ":" in item_name
                            else item_name
                        )

                        data = {
                            "type": "market",
                            "timestamp": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            )
                            + "Z",
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

                def onSubscriptionError(self, code: Any, message: Any) -> None:
                    log.error("Market subscription error: %s - %s", code, message)

                def onSubscription(self) -> None:
                    log.info("Market subscription active")

                def onUnsubscription(self) -> None:
                    log.info("Market unsubscribed")

            market_sub.addListener(MarketListener())
            subscriptions.append(market_sub)

        if chart_subs:
            for chart_sub in chart_subs:
                item_name = chart_sub.get_item_name()
                ls_sub = Subscription(
                    mode="MERGE",
                    items=[item_name],
                    fields=chart_sub.get_fields(),
                )

                def make_chart_listener(sub: ChartSubscription) -> Any:
                    class ChartListener:
                        def onItemUpdate(self, update: Any) -> None:
                            try:
                                cons_end = update.getValue("CONS_END")
                                if cons_end != "1":
                                    return

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

                                open_price = (
                                    float(ofr_open or ofr_close)
                                    + float(bid_open or bid_close)
                                ) / 2
                                high_price = (
                                    float(ofr_high or ofr_close)
                                    + float(bid_high or bid_close)
                                ) / 2
                                low_price = (
                                    float(ofr_low or ofr_close)
                                    + float(bid_low or bid_close)
                                ) / 2
                                close_price = (float(ofr_close) + float(bid_close)) / 2

                                ltv = update.getValue("LTV")
                                tick_count = update.getValue("CONS_TICK_COUNT")

                                volume = float(ltv) if ltv else 0.0
                                ticks = int(tick_count) if tick_count else 0

                                data = {
                                    "type": "chart",
                                    "epic": sub.epic,
                                    "period": sub.period,
                                    "candle": {
                                        "timestamp": update.getValue("UTM")
                                        or datetime.now(timezone.utc).isoformat(),
                                        "open": open_price,
                                        "high": high_price,
                                        "low": low_price,
                                        "close": close_price,
                                        "volume": volume,
                                        "tick_count": ticks,
                                    },
                                }

                                loop.call_soon_threadsafe(chart_queue.put_nowait, data)
                            except Exception as e:
                                log.exception("Error processing chart update: %s", e)

                        def onSubscriptionError(self, code: Any, message: Any) -> None:
                            log.error(
                                "Chart subscription error for %s: %s - %s",
                                sub.epic,
                                code,
                                message,
                            )

                        def onSubscription(self) -> None:
                            log.info(
                                "Chart subscription active for %s %s",
                                sub.epic,
                                sub.period,
                            )

                        def onUnsubscription(self) -> None:
                            log.info(
                                "Chart unsubscribed for %s %s", sub.epic, sub.period
                            )

                    return ChartListener()

                ls_sub.addListener(make_chart_listener(chart_sub))
                subscriptions.append(ls_sub)

        class ConnectionListener:
            def onStatusChange(self, status: Any) -> None:
                log.info("Lightstreamer connection status: %s", status)

            def onServerError(self, code: Any, message: Any) -> None:
                log.error("Lightstreamer server error: %s - %s", code, message)

        ls_client.addListener(ConnectionListener())

        ls_client.connect()
        for sub in subscriptions:
            ls_client.subscribe(sub)

        log.info("Lightstreamer subscriptions active")

        def _period_seconds(period: str) -> int:
            p = period.strip().upper()
            if p == "SECOND":
                return 1
            if p == "HOUR":
                return 60 * 60
            if p.endswith("MINUTE"):
                n = int(p[:-6])  # strip "MINUTE"
                return n * 60
            raise ValueError(f"Unsupported period for heartbeat: {period!r}")

        # Heartbeat tuning: candle subscriptions can legitimately be silent for up to one bar.
        # If we are chart-only (no tick/market updates), raise the watchdog threshold based
        # on the smallest subscribed bar to avoid false positives.
        if chart_subs and not market_subs:
            min_bar_s = min(_period_seconds(s.period) for s in chart_subs)
            tuned = max(float(strategy.watchdog_threshold), float(min_bar_s) * 1.2)
            if tuned != strategy.watchdog_threshold:
                strategy.watchdog_threshold = tuned
                log.info(
                    "Heartbeat tuned for chart-only stream: threshold=%.1fs (min_bar=%ds)",
                    strategy.watchdog_threshold,
                    min_bar_s,
                )

        async def _heartbeat_monitor() -> None:
            while True:
                await asyncio.sleep(self.heartbeat_sleep)
                delta = (
                    datetime.now(timezone.utc) - strategy.last_update
                ).total_seconds()
                if delta > strategy.watchdog_threshold:
                    log.warning(
                        "❤  Heartbeat Alert: no updates for %s in %.1fs. Connection may be stale.",
                        strategy.__class__.__name__,
                        delta,
                    )
                elif delta < self.heartbeat_sleep:
                    log.debug("❤  OK: Last update %.1fs ago", delta)

        async def market_consumer() -> None:
            while True:
                payload = await market_queue.get()
                try:
                    event = MarketData(
                        epic=payload["epic"],
                        bid=payload["bid"],
                        offer=payload["offer"],
                        timestamp=payload["timestamp"],
                        raw=payload["raw"],
                    )
                    await strategy._handle_event(event)
                except Exception:
                    log.exception(
                        "Unhandled exception in market_consumer for %s",
                        payload.get("epic"),
                    )

        async def chart_consumer() -> None:
            while True:
                payload = await chart_queue.get()
                try:
                    candle_data = payload["candle"]
                    candle = Candle(**candle_data)
                    event = CandleClose(
                        epic=payload["epic"],
                        period=payload["period"],
                        candle=candle,
                    )
                    await strategy._handle_event(event)
                except Exception:
                    log.exception(
                        "Unhandled exception in chart_consumer for epic=%s period=%s payload=%r",
                        payload.get("epic"),
                        payload.get("period"),
                        payload,
                    )

        tasks = [asyncio.create_task(_heartbeat_monitor())]

        if market_subs:
            tasks.append(asyncio.create_task(market_consumer()))
        if chart_subs:
            tasks.append(asyncio.create_task(chart_consumer()))

        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            log.info(
                "%s cancelled – cleaning up Lightstreamer", strategy.__class__.__name__
            )
        finally:
            for task in tasks:
                task.cancel()
            await self.disconnect()
