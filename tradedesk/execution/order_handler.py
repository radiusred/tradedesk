"""Event-driven order execution.

Strategies publish ``OrderRequestEvent`` via ``request_order()`` and await
the result.  ``OrderExecutionHandler`` subscribes to those events, executes
them through a ``Client``, and resolves the corresponding Future so the
caller unblocks.

Usage::

    # At startup (runner creates the handler once)
    handler = OrderExecutionHandler(client)

    # In strategy code
    from tradedesk.execution.order_handler import request_order

    result = await request_order(OrderRequest(
        instrument="USDJPY", direction="BUY", size=1.0,
    ))
"""

import asyncio
import logging
import uuid
from typing import Any

from tradedesk.execution import Client, OrderCompletedEvent, OrderRequestEvent

from ..events import DomainEvent, get_dispatcher
from ..types import OrderRequest, OrderResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level registry: request_id → Future[OrderResult]
# ---------------------------------------------------------------------------

_pending_orders: dict[str, "asyncio.Future[OrderResult]"] = {}


async def request_order(request: OrderRequest, *, timeout: float = 30.0) -> OrderResult:
    """Publish an ``OrderRequestEvent`` and await the execution result.

    This is the public API for strategies to place orders without depending
    on ``Client`` directly.

    Args:
        request: The order to place.
        timeout: Maximum seconds to wait for the handler to resolve the
            Future.  In normal operation the handler resolves synchronously
            within ``publish()``, so this is a safety net.
    """
    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    future: asyncio.Future[OrderResult] = loop.create_future()
    _pending_orders[request_id] = future

    await get_dispatcher().publish(OrderRequestEvent(request=request, request_id=request_id))

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        log.error(
            "Order request %s timed out after %.1fs — no handler resolved it",
            request_id,
            timeout,
        )
        return OrderResult(success=False, error="order request timed out")
    finally:
        _pending_orders.pop(request_id, None)


# ---------------------------------------------------------------------------
# Handler (created once per runner / session)
# ---------------------------------------------------------------------------


class OrderExecutionHandler:
    """Subscribes to ``OrderRequestEvent`` and executes orders via a Client.

    Parameters
    ----------
    client:
        The broker client used to place orders and fetch market data.
    spread_limits:
        Optional mapping of instrument (epic) to maximum permitted raw spread
        (offer − bid in price units).  When set, orders are blocked if the
        current spread exceeds the threshold.
    """

    def __init__(
        self,
        client: Client,
        spread_limits: dict[str, float] | None = None,
    ) -> None:
        self._client = client
        self._spread_limits = spread_limits or {}
        get_dispatcher().subscribe(OrderRequestEvent, self._on_order_request)

    async def _on_order_request(self, event: DomainEvent) -> None:
        assert isinstance(event, OrderRequestEvent)
        result = await self._execute(event.request)

        # Resolve the caller's Future
        future = _pending_orders.get(event.request_id)
        if future is not None and not future.done():
            future.set_result(result)

        # Publish completion for audit / logging observers
        await get_dispatcher().publish(
            OrderCompletedEvent(request_id=event.request_id, result=result)
        )

    async def _check_spread(self, instrument: str) -> str | None:
        """Check if current spread exceeds the configured limit.

        Returns an error message if the spread is too wide, or ``None`` if OK.
        """
        max_spread = self._spread_limits.get(instrument)
        if max_spread is None:
            return None

        try:
            snapshot = await self._client.get_market_snapshot(instrument)
        except Exception as exc:
            log.warning(
                "Spread gate: could not fetch snapshot for %s — allowing order: %s",
                instrument,
                exc,
            )
            return None

        snap = snapshot.get("snapshot") or {}
        bid_raw = snap.get("bid")
        offer_raw = snap.get("offer")

        if bid_raw is None or offer_raw is None:
            log.warning(
                "Spread gate: no bid/offer in snapshot for %s — allowing order",
                instrument,
            )
            return None

        bid = float(bid_raw)
        offer = float(offer_raw)
        spread = offer - bid

        if spread > max_spread:
            return (
                f"Spread gate blocked order: {instrument} "
                f"spread {spread:.6f} exceeds limit {max_spread:.6f} "
                f"(bid={bid}, offer={offer})"
            )
        return None

    async def _execute(self, request: OrderRequest) -> OrderResult:
        try:
            # Spread gate: block if current spread is too wide
            spread_error = await self._check_spread(request.instrument)
            if spread_error is not None:
                log.warning(spread_error)
                return OrderResult(success=False, error=spread_error)

            q_size = await self._client.quantise_size(request.instrument, request.size)

            resp: dict[str, Any] = await self._client.place_market_order_confirmed(
                instrument=request.instrument,
                direction=request.direction,
                size=q_size,
                currency=request.currency,
                force_open=request.force_open,
                exit_reason=request.exit_reason,
            )

            # Extract fill price (IG uses "level"; others may use "price")
            val = resp.get("level") or resp.get("price")
            fill_price = float(val) if val is not None else 0.0

            # Prefer broker-reported size when available
            resp_size = resp.get("size")
            if resp_size is not None:
                try:
                    fill_size = float(resp_size)
                except (TypeError, ValueError):
                    fill_size = q_size
            else:
                fill_size = q_size

            return OrderResult(
                success=True,
                fill_price=fill_price,
                fill_size=fill_size,
                raw=resp,
            )
        except Exception as e:
            log.error("Order execution failed for %s: %s", request.instrument, e)
            return OrderResult(success=False, error=str(e))
