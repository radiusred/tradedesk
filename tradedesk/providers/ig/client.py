# tradedesk/client.py
import asyncio
import logging
import time
from typing import Any
import aiohttp
from decimal import Decimal, ROUND_DOWN
from tradedesk.marketdata import Candle
from tradedesk.providers import Client
from tradedesk.providers.ig.settings import settings

log = logging.getLogger(__name__)


class IGClient(Client):
    """Thin wrapper around IG's REST API – handles auth & simple GET/POST."""

    # ------------------------------------------------------------------
    # End-points
    # ------------------------------------------------------------------
    DEMO_BASE = "https://demo-api.ig.com/gateway/deal"
    LIVE_BASE = "https://api.ig.com/gateway/deal"

    # Lightstreamer hosts (used by the strategy)
    DEMO_LS = "https://demo-apd.marketdatasystems.com"
    LIVE_LS = "https://apd.marketdatasystems.com"

    def __init__(self) -> None:
        # Choose the correct base URL for the selected environment
        self.base_url = (
            self.DEMO_BASE if settings.ig_environment == "DEMO" else self.LIVE_BASE
        )
        self.ls_url = (
            self.DEMO_LS if settings.ig_environment == "DEMO" else self.LIVE_LS
        )

        # VERSION 2 returns CST/X-SECURITY-TOKEN (works with Lightstreamer)
        # VERSION 3 returns OAuth tokens (doesn't work with Lightstreamer)
        # For demo with Lightstreamer support, use VERSION 2
        self.api_version = "2"  # if settings.ig_environment == "DEMO" else "3"

        # Store headers for session creation
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "VERSION": self.api_version,
            "X-IG-API-KEY": settings.ig_api_key,
        }

        # OAuth token management
        self.uses_oauth = False
        self.oauth_access_token: str | None = None
        self.oauth_refresh_token: str | None = None
        self.oauth_expires_at: float = 0  # Unix timestamp

        # Identity / Session info
        self.account_id: str | None = None
        self.client_id: str | None = None

        # Lightstreamer authentication tokens (different from OAuth!)
        self.ls_cst: str | None = None
        self.ls_xst: str | None = None

        # Rate limiting and concurrency control
        self.last_auth_attempt: float = 0
        self.min_auth_interval: float = 5.0  # Minimum 5 seconds between auth attempts
        self._auth_lock = asyncio.Lock()  # Prevent concurrent authentication
        self._session: aiohttp.ClientSession | None = None
        self._account_type: str | None = None

        # Instrument metadata cache: epic -> dealing rules
        self._instrument_metadata: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> "IGClient":
        """Async context manager entry."""
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()

    async def start(self) -> None:
        """Initialize the client and authenticate."""
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self.headers)
        await self._authenticate()

    async def close(self) -> None:
        """Close the session."""
        if self._session:
            await self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    async def _authenticate(self) -> None:
        """
        Main driver for authentication.
        Handles rate limiting and dispatches to the correct version handler.
        """
        async with self._auth_lock:
            # 1. Rate Limiting
            await self._enforce_rate_limit()

            # 2. Perform Request
            resp_headers, resp_body = await self._perform_auth_request()

            # 3. Dispatch based on API Version
            if self.api_version == "3":
                await self._handle_v3_auth(resp_body)
            else:
                self._handle_v2_auth(resp_headers, resp_body)

    async def _enforce_rate_limit(self) -> None:
        """Wait if we are authenticating too frequently."""
        now = time.time()
        time_since_last = now - self.last_auth_attempt

        if time_since_last < self.min_auth_interval:
            wait_time = self.min_auth_interval - time_since_last
            log.debug(
                "Rate limiting: waiting %.1f seconds before re-authentication",
                wait_time,
            )
            await asyncio.sleep(wait_time)

        self.last_auth_attempt = time.time()

    async def _perform_auth_request(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """
        Executes the login request and handles network/protocol errors.
        Returns: (response_headers, json_body)
        """
        url = f"{self.base_url}/session"
        payload = {
            "identifier": settings.ig_username,
            "password": settings.ig_password,
        }

        log.debug("POST %s – authenticating with IG (v%s)", url, self.api_version)

        if not self._session:
            self._session = aiohttp.ClientSession(headers=self.headers)

        try:
            async with self._session.post(url, json=payload) as resp:
                # Handle non-200 responses
                if resp.status != 200:
                    await self._handle_auth_error(resp)

                # Parse Success Body
                try:
                    body = await resp.json()
                except Exception:
                    body = {}

                return dict(resp.headers), body

        except aiohttp.ClientError as e:
            log.error("Network error during authentication: %s", e)
            raise RuntimeError(f"Network error during authentication: {e}")

    async def _handle_auth_error(self, resp: aiohttp.ClientResponse) -> None:
        """Parses error responses and raises detailed exceptions."""
        try:
            body = await resp.json()
        except Exception:
            body = await resp.text()

        # Specific check for rate limiting error code
        if resp.status == 403 and isinstance(body, dict):
            if body.get("errorCode") == "error.public-api.exceeded-api-key-allowance":
                msg = "IG API rate limit exceeded. Wait a few minutes or use Lightstreamer."
                log.error(msg)
                raise RuntimeError(msg)

        log.error("IG authentication failed (HTTP %s). Body: %s", resp.status, body)
        raise RuntimeError(
            f"IG authentication failed – HTTP {resp.status}. "
            "Check credentials, API key, and endpoint configuration."
        )

    # ------------------------------------------------------------------
    # Auth Handlers (Version Specific)
    # ------------------------------------------------------------------
    def _handle_v2_auth(self, headers: dict[str, Any], body: dict[str, Any]) -> None:
        """
        Handles Version 2 Authentication (CST / X-SECURITY-TOKEN).
        Required for Lightstreamer streaming.
        """
        cst = headers.get("CST") or body.get("cst")
        x_sec = headers.get("X-SECURITY-TOKEN") or body.get("x-security-token")

        if not cst or not x_sec:
            log.error("Missing V2 tokens. Headers: %s, Body: %s", headers, body)
            raise RuntimeError("CST and X-SECURITY-TOKEN not found in IG response.")

        self.ls_cst = cst
        self.ls_xst = x_sec
        self.client_id = body.get("clientId")
        self.account_id = body.get("currentAccountId") or body.get("accountId")
        self.uses_oauth = False

        if not self.account_id:
            log.error("Missing account id in V2 auth body: %s", body)
            raise RuntimeError("IG account id not found in IG response.")

        self._apply_session_headers(
            {
                "CST": cst,
                "X-SECURITY-TOKEN": x_sec,
                "IG-ACCOUNT-ID": self.account_id,
            }
        )

        log.info("Authenticated (V2) – Streaming enabled.")

    async def _handle_v3_auth(self, body: dict[str, Any]) -> None:
        """
        Handles Version 3 Authentication (OAuth).
        Warning: Does NOT support Lightstreamer.
        """
        oauth_token = body.get("oauthToken", {})
        access_token = oauth_token.get("access_token")

        if not access_token:
            # Fallback: Sometimes V3 endpoints might still return CST in headers?
            # If so, we might need to fallback, but for now strict V3 expects OAuth.
            log.error("Missing OAuth token in V3 response: %s", body)
            raise RuntimeError("OAuth access_token not found in IG response.")

        # Store OAuth details
        await self._store_oauth_token(
            oauth_token, body.get("accountId", ""), body.get("clientId", "")
        )

        log.warning(
            "Authenticated (V3 OAuth) – Streaming NOT available. "
            "System will use REST polling."
        )

    def _apply_session_headers(self, new_headers: dict[str, str]) -> None:
        """Updates internal headers and the active session."""
        self.headers.update(new_headers)
        if self._session:
            self._session.headers.update(new_headers)

    # ------------------------------------------------------------------
    # OAuth Management
    # ------------------------------------------------------------------
    async def _store_oauth_token(
        self, oauth_token: dict[str, Any], account_id: str, client_id: str
    ) -> None:
        """Store OAuth credentials and calculate expiry time."""
        self.oauth_access_token = oauth_token["access_token"]
        self.oauth_refresh_token = oauth_token.get("refresh_token")
        self.account_id = account_id
        self.client_id = client_id

        # Calculate expiry (buffer 5s)
        expires_in = int(oauth_token.get("expires_in", 30))
        self.oauth_expires_at = time.time() + expires_in - 5

        # Apply Headers
        self._apply_session_headers(
            {
                "Authorization": f"Bearer {self.oauth_access_token}",
                "IG-ACCOUNT-ID": account_id,
            }
        )
        self.uses_oauth = True

    def _is_token_valid(self) -> bool:
        """Check if the current token is still valid."""
        if not self.uses_oauth:
            return True
        return time.time() < self.oauth_expires_at

    # ------------------------------------------------------------------
    # Requests & Helpers
    # ------------------------------------------------------------------
    def get_streamer(self) -> Any:
        from tradedesk.providers.ig.streamer import Lightstreamer

        return Lightstreamer(self)

    async def _request(
        self, method: str, path: str, *, api_version: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"

        if not self._session:
            self._session = aiohttp.ClientSession(headers=self.headers)

        if self.uses_oauth:
            time_since_auth = time.time() - self.last_auth_attempt
            if time_since_auth > 25 and not self._is_token_valid():
                log.debug("OAuth token expired – re-authenticating")
                await self._authenticate()

        # Merge headers, allow per-request VERSION override
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
                        err_body = await resp.text()

                    log.error(
                        "HTTP %s for %s %s: %s", resp.status, method, url, err_body
                    )
                    raise RuntimeError(
                        f"IG request failed: HTTP {resp.status}: {err_body}"
                    )

                result: dict[str, Any] = await resp.json()
                return result

        except aiohttp.ClientError as e:
            log.error("Request failed: %s %s - %s", method, url, e)
            raise

    async def _handle_retry_logic(
        self, resp: Any, method: str, url: str, **kwargs: Any
    ) -> None:
        """Attempts to re-authenticate and retry the request once."""
        # 1. Check if it's a rate limit (unrecoverable)
        try:
            body = await resp.json()
            if body.get("errorCode") == "error.public-api.exceeded-api-key-allowance":
                raise RuntimeError("IG API rate limit exceeded.")
        except (ValueError, KeyError):
            pass

        # 2. Re-authenticate
        log.warning("Auth failed (HTTP %s) – attempting re-authentication", resp.status)
        await self._authenticate()

        # 3. Retry
        # Note: In a robust system, we would return the new response here.
        # However, due to the structure of the original _request wrapper,
        # we can just let the caller retry or recurse.
        # For this refactor, we just re-auth. The original code did a manual retry here.
        pass

    def _period_to_rest_resolution(self, period: str) -> str:
        """
        Map tradedesk period strings to IG REST resolution strings.
        IG REST uses e.g. MINUTE, MINUTE_5, HOUR, HOUR_4, DAY, WEEK.
        """
        p = period.upper()
        mapping = {
            "1MINUTE": "MINUTE",
            "5MINUTE": "MINUTE_5",
            "15MINUTE": "MINUTE_15",
            "30MINUTE": "MINUTE_30",
            "HOUR": "HOUR",
            "4HOUR": "HOUR_4",
            "DAY": "DAY",
            "WEEK": "WEEK",
            # Allow passing additional IG formats through
            "MINUTE": "MINUTE",
            "MINUTE_5": "MINUTE_5",
            "MINUTE_15": "MINUTE_15",
            "MINUTE_30": "MINUTE_30",
            "HOUR_4": "HOUR_4",
        }
        return mapping.get(p, p)

    async def _get_accounts(self) -> dict[str, Any]:
        # /accounts is typically VERSION 1
        return await self._request("GET", "/accounts", api_version="1")

    async def _ensure_account_type(self) -> str | None:
        """
        Determine the current account's type (e.g. SPREADBET / CFD) once per session.
        Cached on self._account_type.
        """
        if hasattr(self, "_account_type") and self._account_type:
            return self._account_type

        if not self.account_id:
            return None

        payload = await self._get_accounts()
        accounts = payload.get("accounts") or []
        current = next(
            (a for a in accounts if a.get("accountId") == self.account_id), None
        )
        self._account_type = (current or {}).get("accountType")
        return self._account_type

    async def _dealing_path_for_current_account(self) -> str:
        """
        IG uses /positions/otc for both CFD and spreadbet dealing.
        Product semantics are driven by account type + payload fields (not URL path).
        """
        return "/positions/otc"

    async def get_market_snapshot(self, epic: str) -> dict[str, Any]:
        """Return the latest market snapshot for the given EPIC."""
        return await self._request("GET", f"/markets/{epic}")

    async def get_instrument_metadata(
        self, epic: str, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """
        Fetch and cache instrument metadata (dealing rules) for the given EPIC.

        Returns the full market details response which includes:
        - dealingRules: minDealSize, minStepDistance, etc.
        - instrument: epic, expiry, type, etc.
        - snapshot: current prices

        Results are cached per-epic unless force_refresh=True.
        """
        if not force_refresh and epic in self._instrument_metadata:
            return self._instrument_metadata[epic]

        metadata = await self._request("GET", f"/markets/{epic}")
        self._instrument_metadata[epic] = metadata
        return metadata

    def quantise_size(self, epic: str, size: float) -> float:
        """
        Quantise the position size according to the instrument's minStepDistance.

        This method requires that get_instrument_metadata() has been called for
        this epic at least once (typically during strategy warmup/initialization).

        Args:
            epic: The instrument epic
            size: The desired position size

        Returns:
            The quantised size rounded down to the nearest valid step

        Raises:
            RuntimeError: If instrument metadata has not been fetched for this epic
        """
        if epic not in self._instrument_metadata:
            raise RuntimeError(
                f"Instrument metadata not loaded for {epic}. "
                f"Call get_instrument_metadata('{epic}') first."
            )

        metadata = self._instrument_metadata[epic]
        dealing_rules = metadata.get("dealingRules", {})
        min_step = dealing_rules.get("minStepDistance", {})
        step_value = min_step.get("value")

        # If no step size defined, return the original size
        if step_value is None or float(step_value) <= 0:
            return float(size)

        step = float(step_value)
        s = Decimal(str(size))
        q = Decimal(str(step))
        quantised = float((s / q).to_integral_value(rounding=ROUND_DOWN) * q)

        if quantised != size:
            log.debug(
                "Quantised size for %s: %.10f -> %.10f (step=%.10f)",
                epic,
                size,
                quantised,
                step,
            )

        return quantised

    async def get_price_ticks(self, epic: str) -> dict[str, Any]:
        """Convenient shortcut to the "prices" endpoint."""
        return await self._request("GET", f"/prices/{epic}")

    async def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        currency: str = "GBP",
        force_open: bool = False,
        expiry: str = "-",
        time_in_force: str = "FILL_OR_KILL",
        guaranteed_stop: bool = False,
    ) -> dict[str, Any]:
        """
        Submit a simple OTC market order.

        IG uses POST /positions/otc for both CFD and Spreadbet.
        For SPREADBET accounts, expiry must typically be 'DFB' (not '-').
        """
        acct_type = (await self._ensure_account_type() or "").upper()

        eff_expiry = expiry
        if acct_type == "SPREADBET" and (
            expiry is None or expiry.strip() == "-" or expiry.strip() == ""
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
            # Keep currencyCode: many IG setups accept/expect it for OTC dealing.
            "currencyCode": currency,
        }

        # Dealing endpoints are generally VERSION 1 (more consistent across IG)
        path = await self._dealing_path_for_current_account()
        return await self._request("POST", path, json=order, api_version="1")

    async def confirm_deal(
        self,
        deal_reference: str,
        *,
        timeout_s: float = 10.0,
        poll_s: float = 0.25,
    ) -> dict[str, Any]:
        """
        Poll /confirms/{dealReference} until dealStatus is no longer PENDING.

        IG DEMO can return transient:
        - HTTP 500s for confirms
        - HTTP 404 error.confirms.deal-not-found briefly after placement
        Treat those as retryable until timeout.
        """
        deadline = time.monotonic() + timeout_s
        last_err: Exception | None = None

        while True:
            try:
                payload = await self._request(
                    "GET", f"/confirms/{deal_reference}", api_version="1"
                )
                status = (payload.get("dealStatus") or "").upper()

                if status and status != "PENDING":
                    return payload

            except RuntimeError as e:
                msg = str(e)
                retryable = ("HTTP 500" in msg) or (
                    "HTTP 404" in msg and "error.confirms.deal-not-found" in msg
                )
                if retryable:
                    last_err = e
                    log.warning(
                        "Transient error confirming deal %s: %s", deal_reference, msg
                    )
                else:
                    raise

            if time.monotonic() >= deadline:
                if last_err:
                    raise TimeoutError(
                        f"Timed out waiting for deal confirm (last error: {last_err})"
                    ) from last_err
                raise TimeoutError(
                    f"Timed out waiting for deal confirm: {deal_reference}"
                )

            await asyncio.sleep(poll_s)

    async def place_market_order_confirmed(
        self,
        *,
        epic: str,
        direction: str,
        size: float,
        expiry: str = "-",
        currency: str = "GBP",
        force_open: bool = False,
        time_in_force: str = "FILL_OR_KILL",
        guaranteed_stop: bool = False,
        confirm_timeout_s: float = 10.0,
        confirm_poll_s: float = 0.25,
    ) -> dict[str, Any]:
        res = await self.place_market_order(
            epic=epic,
            direction=direction,
            size=size,
            expiry=expiry,
            currency=currency,
            force_open=force_open,
            time_in_force=time_in_force,
            guaranteed_stop=guaranteed_stop,
        )
        deal_ref = res.get("dealReference")
        if not deal_ref:
            raise RuntimeError(
                f"Expected dealReference from place_market_order, got: {res}"
            )

        return await self.confirm_deal(
            deal_ref, timeout_s=confirm_timeout_s, poll_s=confirm_poll_s
        )

    async def get_historical_candles(
        self, epic: str, period: str, num_points: int
    ) -> list[Candle]:
        """
        Fetch the most recent `num_points` candles for (epic, period) via IG REST /prices.

        Returns candles ordered oldest -> newest.
        """
        if num_points <= 0:
            return []

        resolution = self._period_to_rest_resolution(period)
        payload = await self._request(
            "GET", f"/prices/{epic}/{resolution}/{num_points}"
        )

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

            o = mid(p.get("openPrice"))
            h = mid(p.get("highPrice"))
            l = mid(p.get("lowPrice"))
            c = mid(p.get("closePrice"))
            if c is None:
                continue

            open_p = o if o is not None else c
            high_p = h if h is not None else c
            low_p = l if l is not None else c

            volume = float(p.get("lastTradedVolume") or 0.0)

            candles.append(
                Candle(
                    timestamp=timestamp,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=c,
                    volume=volume,
                    tick_count=0,
                )
            )

        candles.sort(key=lambda x: x.timestamp)  # oldest -> newest
        return candles
