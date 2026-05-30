# tradedesk/execution/ig/client.py
"""IG API client — thin orchestrator over focused sub-components."""
from __future__ import annotations

import logging
import re
from typing import Any

import aiohttp

from tradedesk.execution.broker import (
    AccountBalance,
    BrokerPosition,
    HistoricalDataAllowanceError,
)
from tradedesk.execution.client import Client
from tradedesk.settings import IG_DEAL_CONFIRM_POLL_S, IG_DEAL_CONFIRM_TIMEOUT_S
from tradedesk.types import Candle

from .auth import IGAuthManager
from .metadata import IGMetadataCache
from .orders import IGOrderHandler
from .positions import IGPositionTracker
from .price_streamer import Lightstreamer
from .settings import settings

log = logging.getLogger(__name__)

_ERROR_BODY_TRUNCATE = 200


class IGClient(Client):
    """Thin wrapper around IG's REST API – delegates to focused sub-components."""

    DEMO_BASE = "https://demo-api.ig.com/gateway/deal"
    LIVE_BASE = "https://api.ig.com/gateway/deal"
    DEMO_LS = "https://demo-apd.marketdatasystems.com"
    LIVE_LS = "https://apd.marketdatasystems.com"

    def __init__(self) -> None:
        self.base_url = self.DEMO_BASE if settings.ig_environment == "DEMO" else self.LIVE_BASE
        self.ls_url = self.DEMO_LS if settings.ig_environment == "DEMO" else self.LIVE_LS
        self.api_version = "2"

        self.headers: dict[str, str] = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "VERSION": self.api_version,
            "X-IG-API-KEY": settings.ig_api_key,
        }
        self._session: aiohttp.ClientSession | None = None
        self._account_type: str | None = None

        # Sub-components — settings passed so auth patches in tests remain effective
        self.auth = IGAuthManager(self, settings)
        self._metadata = IGMetadataCache(self)
        self._positions = IGPositionTracker(self)
        self._orders = IGOrderHandler(self)

    # ------------------------------------------------------------------
    # Backward-compatible property forwarding from IGAuthManager
    # ------------------------------------------------------------------

    @property
    def account_id(self) -> str | None:
        return self.auth.account_id

    @account_id.setter
    def account_id(self, value: str | None) -> None:
        self.auth.account_id = value

    @property
    def client_id(self) -> str | None:
        return self.auth.client_id

    @property
    def ls_cst(self) -> str | None:
        return self.auth.ls_cst

    @property
    def ls_xst(self) -> str | None:
        return self.auth.ls_xst

    @property
    def uses_oauth(self) -> bool:
        return self.auth.uses_oauth

    @uses_oauth.setter
    def uses_oauth(self, value: bool) -> None:
        self.auth.uses_oauth = value

    @property
    def oauth_access_token(self) -> str | None:
        return self.auth.oauth_access_token

    @property
    def oauth_refresh_token(self) -> str | None:
        return self.auth.oauth_refresh_token

    @property
    def oauth_expires_at(self) -> float:
        return self.auth.oauth_expires_at

    @oauth_expires_at.setter
    def oauth_expires_at(self, value: float) -> None:
        self.auth.oauth_expires_at = value

    @property
    def last_auth_attempt(self) -> float:
        return self.auth.last_auth_attempt

    @last_auth_attempt.setter
    def last_auth_attempt(self, value: float) -> None:
        self.auth.last_auth_attempt = value

    def _is_token_valid(self) -> bool:
        return self.auth.is_token_valid()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def __aenter__(self) -> IGClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def start(self) -> None:
        """Initialize the client and authenticate."""
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self.headers)
        try:
            await self._authenticate()
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Auth — kept for backward compat with tests that mock _authenticate
    # ------------------------------------------------------------------

    async def _authenticate(self) -> None:
        await self.auth.authenticate()

    # ------------------------------------------------------------------
    # HTTP transport
    # ------------------------------------------------------------------

    def _apply_session_headers(self, new_headers: dict[str, str]) -> None:
        self.headers.update(new_headers)
        if self._session:
            self._session.headers.update(new_headers)

    async def _request(
        self, method: str, path: str, *, api_version: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"

        if not self._session:
            self._session = aiohttp.ClientSession(headers=self.headers)

        if self.auth.uses_oauth:
            await self.auth.ensure_valid()

        req_headers: dict[str, str] = dict(self._session.headers)
        caller_headers = kwargs.pop("headers", None)
        if caller_headers:
            req_headers.update(dict(caller_headers))
        if api_version is not None:
            req_headers["VERSION"] = str(api_version)

        try:
            async with self._session.request(
                method, url, headers=req_headers, **kwargs
            ) as resp:
                if resp.status in (401, 403):
                    await self._handle_retry_logic(
                        resp, method, url, headers=req_headers, **kwargs
                    )

                if resp.status >= 400:
                    try:
                        err_body = await resp.json()
                    except Exception:
                        raw = await resp.text()
                        if "<html" in raw.lower():
                            err_body = re.sub(r"<[^>]+>", " ", raw)
                            err_body = " ".join(err_body.split())[:_ERROR_BODY_TRUNCATE]
                        else:
                            err_body = raw
                    raise RuntimeError(f"IG request failed: HTTP {resp.status}: {err_body}")

                result = await resp.json()
                return result if isinstance(result, dict) else {}

        except aiohttp.ClientError as e:
            log.error("Request failed: %s %s - %s", method, url, e)
            raise

    async def _handle_retry_logic(
        self, resp: Any, method: str, url: str, **kwargs: Any
    ) -> None:
        """Attempt re-authentication on 401/403; raise immediately on rate limit."""
        try:
            body = await resp.json()
            if isinstance(body, dict):
                error_code = body.get("errorCode", "")
                if error_code == "error.public-api.exceeded-api-key-allowance":
                    raise RuntimeError("IG API rate limit exceeded.")
                if error_code == "error.public-api.exceeded-account-historical-data-allowance":
                    raise HistoricalDataAllowanceError(
                        "IG historical data allowance exceeded for this account."
                    )
        except (ValueError, KeyError):
            pass
        except (RuntimeError, HistoricalDataAllowanceError):
            raise

        log.warning("Auth failed (HTTP %s) – attempting re-authentication", resp.status)
        await self._authenticate()

    async def _get_accounts(self) -> dict[str, Any]:
        return await self._request("GET", "/accounts", api_version="1")

    async def _ensure_account_type(self) -> str | None:
        """Determine the current account type (e.g. SPREADBET / CFD); cached per session."""
        if self._account_type:
            return self._account_type
        if not self.account_id:
            return None
        payload = await self._get_accounts()
        accounts = payload.get("accounts") or []
        current = next((a for a in accounts if a.get("accountId") == self.account_id), None)
        self._account_type = (current or {}).get("accountType")
        return self._account_type

    # ------------------------------------------------------------------
    # Public API — delegating to sub-components
    # ------------------------------------------------------------------

    def get_streamer(self) -> Any:
        return Lightstreamer(self)

    async def get_market_snapshot(self, instrument: str) -> dict[str, Any]:
        return await self._metadata.get_market_snapshot(instrument)

    async def get_instrument_metadata(
        self, epic: str, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        return await self._metadata.get_instrument_metadata(epic, force_refresh=force_refresh)

    async def quantise_size(self, epic: str, size: float) -> float:
        return await self._metadata.quantise_size(epic, size)

    def _period_to_rest_resolution(self, period: str) -> str:
        return self._metadata.period_to_rest_resolution(period)

    async def get_positions(self) -> list[BrokerPosition]:
        return await self._positions.get_positions()

    async def get_account_balance(self) -> AccountBalance:
        return await self._positions.get_account_balance()

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
        return await self._orders.place_market_order(
            instrument=instrument,
            direction=direction,
            size=size,
            currency=currency,
            force_open=force_open,
            exit_reason=exit_reason,
            expiry=expiry,
            time_in_force=time_in_force,
            guaranteed_stop=guaranteed_stop,
            **kwargs,
        )

    async def confirm_deal(
        self,
        deal_reference: str,
        *,
        timeout_s: float = IG_DEAL_CONFIRM_TIMEOUT_S,
        poll_s: float = IG_DEAL_CONFIRM_POLL_S,
    ) -> dict[str, Any]:
        return await self._orders.confirm_deal(
            deal_reference, timeout_s=timeout_s, poll_s=poll_s
        )

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
        return await self._orders.place_market_order_confirmed(
            instrument=instrument,
            direction=direction,
            size=size,
            currency=currency,
            force_open=force_open,
            exit_reason=exit_reason,
            time_in_force=time_in_force,
            expiry=expiry,
            guaranteed_stop=guaranteed_stop,
            confirm_timeout_s=confirm_timeout_s,
            confirm_poll_s=confirm_poll_s,
            **kwargs,
        )

    async def get_historical_candles(
        self, instrument: str, period: str, num_points: int
    ) -> list[Candle]:
        """Fetch the most recent num_points candles for (instrument, period) via IG REST."""
        epic = instrument
        if num_points <= 0:
            return []

        resolution = self._metadata.period_to_rest_resolution(period)
        payload = await self._request("GET", f"/prices/{epic}/{resolution}/{num_points}")
        prices = payload.get("prices") or []
        candles: list[Candle] = []

        def mid(price_obj: Any) -> float | None:
            if not isinstance(price_obj, dict):
                return None
            bid = price_obj.get("bid")
            ask = price_obj.get("ask")
            if bid is None or ask is None:
                return None
            return (float(bid) + float(ask)) / 2.0

        for p in prices:
            ts = p.get("snapshotTimeUTC") or p.get("snapshotTime")
            if not ts:
                continue
            timestamp = ts if ts.endswith("Z") else ts + "Z"

            open_ = mid(p.get("openPrice"))
            high = mid(p.get("highPrice"))
            low = mid(p.get("lowPrice"))
            close = mid(p.get("closePrice"))
            if close is None:
                continue

            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=open_ if open_ is not None else close,
                    high=high if high is not None else close,
                    low=low if low is not None else close,
                    close=close,
                    volume=float(p.get("lastTradedVolume") or 0.0),
                    tick_count=0,
                )
            )

        candles.sort(key=lambda x: x.timestamp)
        return candles

    # ------------------------------------------------------------------
    # Kept for backward compatibility
    # ------------------------------------------------------------------

    @property
    def _instrument_metadata(self) -> dict[str, Any]:
        """Backward-compatible access to the metadata cache dict."""
        return self._metadata._cache

    async def _dealing_path_for_current_account(self) -> str:
        return "/positions/otc"

    async def get_price_ticks(self, epic: str) -> dict[str, Any]:
        return await self._request("GET", f"/prices/{epic}")
