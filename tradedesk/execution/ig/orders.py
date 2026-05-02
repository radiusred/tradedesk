# tradedesk/execution/ig/orders.py
"""IG order placement and deal confirmation."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from tradedesk.execution.broker import DealRejectedException
from tradedesk.settings import IG_DEAL_CONFIRM_POLL_S, IG_DEAL_CONFIRM_TIMEOUT_S

if TYPE_CHECKING:
    from .client import IGClient

log = logging.getLogger(__name__)


class IGOrderHandler:
    """Places and confirms OTC orders via IG REST API."""

    def __init__(self, client: IGClient) -> None:
        self._client = client

    async def place_market_order(
        self,
        instrument: str,
        direction: str,
        size: float,
        currency: str = "GBP",
        force_open: bool = False,
        exit_reason: str = "",
        expiry: str = "-",
        time_in_force: str = "FILL_OR_KILL",
        guaranteed_stop: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Submit a simple OTC market order."""
        epic = instrument
        acct_type = (await self._client._ensure_account_type() or "").upper()

        eff_expiry = expiry
        if acct_type == "SPREADBET" and (
            expiry is None or expiry.strip() in ("-", "")
        ):
            eff_expiry = "DFB"

        order: dict[str, Any] = {
            "epic": epic,
            "expiry": eff_expiry,
            "direction": direction.upper(),
            "size": size,
            "orderType": "MARKET",
            "timeInForce": time_in_force,
            "forceOpen": force_open,
            "guaranteedStop": guaranteed_stop,
            "currencyCode": currency,
        }
        log.info("Placing market order: %s, %s, %s", epic, size, direction)
        log.debug("Order payload: %s", order)
        return await self._client._request("POST", "/positions/otc", json=order, api_version="1")

    async def confirm_deal(
        self,
        deal_reference: str,
        *,
        timeout_s: float = IG_DEAL_CONFIRM_TIMEOUT_S,
        poll_s: float = IG_DEAL_CONFIRM_POLL_S,
    ) -> dict[str, Any]:
        """Poll /confirms/{dealReference} until dealStatus is no longer PENDING."""
        deadline = time.monotonic() + timeout_s
        last_err: Exception | None = None

        while True:
            try:
                payload = await self._client._request(
                    "GET", f"/confirms/{deal_reference}", api_version="1"
                )
                status = (payload.get("dealStatus") or "").upper()

                if status and status != "PENDING":
                    log.info("Order %s confirmed with status: %s", deal_reference, status)
                    return payload

            except RuntimeError as e:
                msg = str(e)
                retryable = ("HTTP 500" in msg) or (
                    "HTTP 404" in msg and "error.confirms.deal-not-found" in msg
                )
                if retryable:
                    last_err = e
                    log.debug("Transient error confirming deal %s: %s", deal_reference, msg)
                else:
                    raise

            if time.monotonic() >= deadline:
                if last_err:
                    raise TimeoutError(
                        f"Timed out waiting for deal confirm (last error: {last_err})"
                    ) from last_err
                raise TimeoutError(f"Timed out waiting for deal confirm: {deal_reference}")

            await asyncio.sleep(poll_s)

    async def place_market_order_confirmed(
        self,
        instrument: str,
        direction: str,
        size: float,
        currency: str = "GBP",
        force_open: bool = False,
        exit_reason: str = "",
        time_in_force: str = "FILL_OR_KILL",
        expiry: str = "-",
        guaranteed_stop: bool = False,
        confirm_timeout_s: float = IG_DEAL_CONFIRM_TIMEOUT_S,
        confirm_poll_s: float = IG_DEAL_CONFIRM_POLL_S,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Place a market order and confirm its execution."""
        res = await self.place_market_order(
            instrument=instrument,
            direction=direction,
            size=size,
            expiry=expiry,
            currency=currency,
            force_open=force_open,
            time_in_force=time_in_force,
            guaranteed_stop=guaranteed_stop,
            **kwargs,
        )
        deal_ref = res.get("dealReference")
        if not deal_ref:
            raise RuntimeError(f"Expected dealReference from place_market_order, got: {res}")

        deal = await self.confirm_deal(
            deal_ref, timeout_s=confirm_timeout_s, poll_s=confirm_poll_s
        )
        if deal.get("dealStatus", "").upper() != "ACCEPTED":
            raise DealRejectedException(f"Deal rejected: {deal}")

        return deal
