import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from tradedesk.execution.streamer import Streamer
from tradedesk.marketdata import (
    CandleClosedEvent,
    ChartSubscription,
    MarketData,
    MarketSubscription,
)
from tradedesk.marketdata.aggregation import period_to_seconds
from tradedesk.settings import (
    STREAM_HEARTBEAT_SLEEP_S,
    STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S,
    STREAM_MAX_STALE_DEFAULT_S,
    STREAM_RECONNECT_DELAY_DEFAULT_S,
    STREAM_SILENCE_SUPPRESS_THRESHOLD_S,
    STREAM_SUB_MAX_RETRIES,
    STREAM_SUB_RETRY_BASE_DELAY_S,
)
from tradedesk.types import Candle

from .metrics import (
    STREAM_RECONNECTS,
    STREAM_STALE_SECONDS,
    SUBSCRIPTION_RETRIES,
)

log = logging.getLogger(__name__)

class RetryScheduler:
    """Schedule subscription retries on the asyncio loop and track pending tasks.

    Lightstreamer listener callbacks are invoked from background threads, so
    retries are dispatched onto the owner loop via ``call_soon_threadsafe``.
    Pending tasks are tracked so the session can cancel all of them cleanly
    on disconnect — replacing the old ``threading.Timer`` approach which had
    no cancellation hook.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

    def schedule(
        self,
        delay: float,
        action: Callable[[], None],
        *,
        kind: str = "unknown",
    ) -> None:
        """Schedule ``action`` to run after ``delay`` seconds on the loop."""
        if self._closed:
            return

        def _spawn() -> None:
            if self._closed:
                return
            task = self._loop.create_task(self._run(delay, action, kind))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        try:
            self._loop.call_soon_threadsafe(_spawn)
        except RuntimeError:
            # Loop already closed — nothing to do.
            pass

    async def _run(
        self, delay: float, action: Callable[[], None], kind: str
    ) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            SUBSCRIPTION_RETRIES.labels(kind=kind).inc()
            action()
        except (RuntimeError, AttributeError, OSError):
            log.exception("Subscription retry action failed (kind=%s)", kind)

    async def cancel_all(self) -> None:
        """Cancel any pending retry tasks and wait for them to finish."""
        self._closed = True
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

# Optional import
try:
    from lightstreamer.client import (  # type: ignore[import-untyped]
        LightstreamerClient,
        Subscription,
    )
except ImportError:  # pragma: no cover
    LightstreamerClient = None
    Subscription = None


class StaleStreamError(RuntimeError):
    """Raised when the Lightstreamer stream has been stale beyond the allowed threshold.

    This error is used internally to signal that reconnection is needed.
    Callers can also catch it if ``auto_reconnect`` is disabled.
    """



class _ConnectionListener:
    def onStatusChange(self, status: Any) -> None:
        log.info("Lightstreamer connection status: %s", status)

    def onServerError(self, code: Any, message: Any) -> None:
        log.error("Lightstreamer server error: %s - %s", code, message)


class _MarketListener:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict[str, Any]]",
        items: list[str],
        ls_client: Any,
        ls_subscription: Any,
        scheduler: RetryScheduler,
    ) -> None:
        self._loop = loop
        self._queue = queue
        self._items = items
        self._ls_client = ls_client
        self._ls_subscription = ls_subscription
        self._scheduler = scheduler
        self._retries = 0

    def onItemUpdate(self, update: Any) -> None:
        try:
            bid_str = update.getValue("BIDPRICE1")
            offer_str = update.getValue("ASKPRICE1")

            if not bid_str or not offer_str:
                return

            item_name = update.getItemName()
            parts = item_name.split(":", 2)
            epic = parts[2] if len(parts) >= 3 else item_name

            data = {
                "type": "market",
                "timestamp": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                )
                + "Z",
                "instrument": epic,
                "bid": float(bid_str),
                "offer": float(offer_str),
                "raw": {
                    "BIDPRICE1": bid_str,
                    "ASKPRICE1": offer_str,
                    "TIMESTAMP": update.getValue("TIMESTAMP"),
                    "DLG_FLAG": update.getValue("DLG_FLAG"),
                },
            }

            self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            log.exception("Error processing market update: %s", e)

    def onSubscriptionError(self, code: Any, message: Any) -> None:
        self._retries += 1
        if self._retries <= STREAM_SUB_MAX_RETRIES:
            delay = self._retries * STREAM_SUB_RETRY_BASE_DELAY_S
            log.warning(
                "Market subscription error (items=%s): %s - %s "
                "— retrying in %.0fs (%d/%d)",
                self._items,
                code,
                message,
                delay,
                self._retries,
                STREAM_SUB_MAX_RETRIES,
            )
            self._scheduler.schedule(
                delay,
                lambda: self._ls_client.subscribe(self._ls_subscription),
                kind="market",
            )
        else:
            log.error(
                "Market subscription error (items=%s): %s - %s "
                "— retries exhausted",
                self._items,
                code,
                message,
            )

    def onSubscription(self) -> None:
        self._retries = 0
        log.info("Market subscription active (items=%s)", self._items)

    def onUnsubscription(self) -> None:
        log.info("Market unsubscribed")


class _ChartListener:
    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: "asyncio.Queue[dict[str, Any]]",
        sub: ChartSubscription,
        ls_client: Any,
        ls_subscription: Any,
        scheduler: RetryScheduler,
    ) -> None:
        self._loop = loop
        self._queue = queue
        self._sub = sub
        self._ls_client = ls_client
        self._ls_subscription = ls_subscription
        self._scheduler = scheduler
        self._retries = 0

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
                "instrument": self._sub.instrument,
                "timeframe": self._sub.period,
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

            self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
        except (ValueError, TypeError, KeyError, AttributeError) as e:
            log.exception("Error processing chart update: %s", e)

    def onSubscriptionError(self, code: Any, message: Any) -> None:
        self._retries += 1
        if self._retries <= STREAM_SUB_MAX_RETRIES:
            delay = self._retries * STREAM_SUB_RETRY_BASE_DELAY_S
            log.warning(
                "Chart subscription error for %s (item=%s): "
                "%s - %s — retrying in %.0fs (%d/%d)",
                self._sub.instrument,
                self._sub.get_item_name(),
                code,
                message,
                delay,
                self._retries,
                STREAM_SUB_MAX_RETRIES,
            )
            self._scheduler.schedule(
                delay,
                lambda: self._ls_client.subscribe(self._ls_subscription),
                kind="chart",
            )
        else:
            log.error(
                "Chart subscription error for %s (item=%s): "
                "%s - %s — retries exhausted",
                self._sub.instrument,
                self._sub.get_item_name(),
                code,
                message,
            )

    def onSubscription(self) -> None:
        self._retries = 0
        log.info(
            "Chart subscription active for %s %s",
            self._sub.instrument,
            self._sub.period,
        )

    def onUnsubscription(self) -> None:
        log.info(
            "Chart unsubscribed for %s %s",
            self._sub.instrument,
            self._sub.period,
        )


class Lightstreamer(Streamer):
    """
    IG Lightstreamer implementation of the provider-neutral Streamer interface.

    This class encapsulates all Lightstreamer-specific wiring and translates
    incoming updates into BaseStrategy callbacks.

    When ``auto_reconnect`` is enabled (the default), the streamer will
    automatically disconnect and reconnect when the stream is stale for
    longer than ``max_stale_seconds``.
    """

    def __init__(
        self,
        client: Any,
        *,
        max_stale_seconds: float = STREAM_MAX_STALE_DEFAULT_S,
        auto_reconnect: bool = True,
        max_reconnect_attempts: int = 0,
        reconnect_delay: float = STREAM_RECONNECT_DELAY_DEFAULT_S,
    ):
        self.client = client
        self._ls_client: Any = None
        self.heartbeat_sleep = STREAM_HEARTBEAT_SLEEP_S
        self.max_stale_seconds = max_stale_seconds
        self.auto_reconnect = auto_reconnect
        self.max_reconnect_attempts = max_reconnect_attempts
        self.reconnect_delay = reconnect_delay
        self._scheduler: RetryScheduler | None = None

    async def connect(self) -> None:
        # Connection is established inside run() to preserve the existing flow.
        return

    async def disconnect(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.cancel_all()
            self._scheduler = None
        if self._ls_client is not None:
            try:
                self._ls_client.disconnect()
            except (RuntimeError, AttributeError, OSError):
                log.exception("Lightstreamer disconnect failed")

    async def run(self, consumer: Any) -> None:
        if LightstreamerClient is None or Subscription is None:
            raise RuntimeError("Lightstreamer client library not available")

        attempts = 0
        while True:
            try:
                await self._run_session(consumer)
                return
            except StaleStreamError:
                attempts += 1
                if not self.auto_reconnect:
                    STREAM_RECONNECTS.labels(reason="stale_no_retry").inc()
                    raise
                if (
                    self.max_reconnect_attempts > 0
                    and attempts >= self.max_reconnect_attempts
                ):
                    STREAM_RECONNECTS.labels(reason="exhausted").inc()
                    log.error(
                        "Reconnect limit reached (%d attempts). Giving up.",
                        attempts,
                    )
                    raise
                STREAM_RECONNECTS.labels(reason="stale").inc()
                log.info(
                    "Reconnecting after stale stream (attempt %d). "
                    "Waiting %.1fs before reconnect.",
                    attempts,
                    self.reconnect_delay,
                )
                await asyncio.sleep(self.reconnect_delay)

    def _create_ls_client(self) -> Any:
        ls_client = LightstreamerClient(self.client.ls_url, "DEFAULT")
        self._ls_client = ls_client

        ls_user = self.client.client_id or self.client.account_id or ""
        ls_client.connectionDetails.setUser(ls_user)
        ls_client.connectionDetails.setPassword(
            f"CST-{self.client.ls_cst}|XST-{self.client.ls_xst}"
        )

        log.info(
            "LS connecting to %s (user=%s, accountId=%s, clientId=%s)",
            self.client.ls_url,
            ls_user,
            self.client.account_id,
            self.client.client_id,
        )

        ls_client.addListener(_ConnectionListener())
        return ls_client

    @staticmethod
    def _setup_subscriptions(
        market_subs: list[MarketSubscription],
        chart_subs: list[ChartSubscription],
        ls_client: Any,
        market_queue: "asyncio.Queue[dict[str, Any]]",
        chart_queue: "asyncio.Queue[dict[str, Any]]",
        loop: asyncio.AbstractEventLoop,
        scheduler: RetryScheduler,
    ) -> list[Any]:
        assert Subscription is not None

        subscriptions: list[Any] = []

        if market_subs:
            market_items = [sub.get_item_name() for sub in market_subs]
            market_sub = Subscription(
                mode="MERGE",
                items=market_items,
                fields=market_subs[0].get_fields(),
            )
            market_sub.setDataAdapter("Pricing")
            market_sub.addListener(
                _MarketListener(
                    loop, market_queue, market_items,
                    ls_client, market_sub, scheduler,
                )
            )
            subscriptions.append(market_sub)

        if chart_subs:
            for cs in chart_subs:
                ls_sub = Subscription(
                    mode="MERGE",
                    items=[cs.get_item_name()],
                    fields=cs.get_fields(),
                )
                ls_sub.addListener(
                    _ChartListener(
                        loop, chart_queue, cs, ls_client, ls_sub, scheduler,
                    )
                )
                subscriptions.append(ls_sub)

        return subscriptions

    @staticmethod
    def _tune_watchdog(
        consumer: Any,
        chart_subs: list[ChartSubscription],
        market_subs: list[MarketSubscription],
    ) -> None:
        # Candle subscriptions can legitimately be silent for up to one bar.
        # For chart-only streams, raise the watchdog threshold based on the
        # smallest subscribed bar to avoid false positives.
        if chart_subs and not market_subs:
            min_bar_s = min(
                period_to_seconds(s.period) for s in chart_subs
            )
            tuned = max(
                float(consumer.watchdog_threshold),
                float(min_bar_s) * 1.2,
            )
            if tuned != consumer.watchdog_threshold:
                consumer.watchdog_threshold = tuned
                log.info(
                    "Heartbeat tuned for chart-only stream: "
                    "threshold=%.1fs (min_bar=%ds)",
                    consumer.watchdog_threshold,
                    min_bar_s,
                )

    async def _heartbeat_monitor(self, consumer: Any) -> None:
        silence_threshold = STREAM_SILENCE_SUPPRESS_THRESHOLD_S
        suppressed = False
        while True:
            sleep = (
                STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S
                if suppressed
                else self.heartbeat_sleep
            )
            await asyncio.sleep(sleep)
            delta = (
                datetime.now(timezone.utc) - consumer.last_update
            ).total_seconds()
            if delta > consumer.watchdog_threshold:
                if (
                    self.max_stale_seconds > 0
                    and delta > self.max_stale_seconds
                ):
                    STREAM_STALE_SECONDS.observe(delta)
                    log.warning(
                        "❤  Stream stale for %.1fs (limit %.1fs). "
                        "Initiating reconnection.",
                        delta,
                        self.max_stale_seconds,
                    )
                    raise StaleStreamError(
                        f"No updates for {delta:.0f}s "
                        f"(max_stale_seconds="
                        f"{self.max_stale_seconds:.0f})"
                    )
                if not suppressed and delta >= silence_threshold:
                    log.warning(
                        "❤  Stream silent for %s (%.0fs). "
                        "Suppressing heartbeat warnings "
                        "until data resumes.",
                        consumer.__class__.__name__,
                        delta,
                    )
                    suppressed = True
                elif not suppressed:
                    log.warning(
                        "❤  Heartbeat Alert: no updates for %s "
                        "in %.1fs. Connection may be stale.",
                        consumer.__class__.__name__,
                        delta,
                    )
            else:
                if suppressed:
                    log.info(
                        "❤  Stream resumed for %s after silence. "
                        "Last update %.1fs ago.",
                        consumer.__class__.__name__,
                        delta,
                    )
                    suppressed = False
                elif delta < self.heartbeat_sleep:
                    log.debug(
                        "❤  OK: Last update %.1fs ago", delta
                    )

    @staticmethod
    async def _consume_market_queue(
        consumer: Any,
        queue: "asyncio.Queue[dict[str, Any]]",
    ) -> None:
        while True:
            payload = await queue.get()
            try:
                event = MarketData(
                    instrument=payload["instrument"],
                    bid=payload["bid"],
                    offer=payload["offer"],
                    timestamp=payload["timestamp"],
                    raw=payload["raw"],
                )
                await consumer._handle_event(event)
            except Exception:
                log.exception(
                    "Unhandled exception in market_consumer for %s",
                    payload.get("instrument"),
                )

    @staticmethod
    async def _consume_chart_queue(
        consumer: Any,
        queue: "asyncio.Queue[dict[str, Any]]",
    ) -> None:
        while True:
            payload = await queue.get()
            try:
                candle_data = payload["candle"]
                candle = Candle(**candle_data)
                event = CandleClosedEvent(
                    instrument=payload["instrument"],
                    timeframe=payload["timeframe"],
                    candle=candle,
                )
                await consumer._handle_event(event)
            except Exception:
                log.exception(
                    "Unhandled exception in chart_consumer "
                    "for epic=%s period=%s payload=%r",
                    payload.get("epic"),
                    payload.get("period"),
                    payload,
                )

    async def _run_session(self, consumer: Any) -> None:
        """Run a single Lightstreamer session."""
        market_subs = [
            s
            for s in consumer.subscriptions
            if isinstance(s, MarketSubscription)
        ]
        chart_subs = [
            s
            for s in consumer.subscriptions
            if isinstance(s, ChartSubscription)
        ]

        log.info(
            "Starting Lightstreamer streaming: "
            "%d chart + %d market subscriptions",
            len(chart_subs),
            len(market_subs),
        )
        for cs in chart_subs:
            log.info("  CHART sub: %s", cs.get_item_name())
        for ms in market_subs:
            log.info("  PRICE sub: %s", ms.get_item_name())

        market_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        chart_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        scheduler = RetryScheduler(loop)
        self._scheduler = scheduler

        ls_client = self._create_ls_client()
        subscriptions = self._setup_subscriptions(
            market_subs, chart_subs, ls_client,
            market_queue, chart_queue, loop, scheduler,
        )

        ls_client.connect()
        for sub in subscriptions:
            ls_client.subscribe(sub)

        log.info("Lightstreamer subscriptions active")

        self._tune_watchdog(consumer, chart_subs, market_subs)

        tasks = [asyncio.create_task(self._heartbeat_monitor(consumer))]
        if market_subs:
            tasks.append(
                asyncio.create_task(
                    self._consume_market_queue(consumer, market_queue)
                )
            )
        if chart_subs:
            tasks.append(
                asyncio.create_task(
                    self._consume_chart_queue(consumer, chart_queue)
                )
            )

        try:
            done, _pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        except asyncio.CancelledError:
            log.info(
                "%s cancelled – cleaning up Lightstreamer",
                consumer.__class__.__name__,
            )
        finally:
            for task in tasks:
                task.cancel()
            await self.disconnect()
