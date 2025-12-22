# tradedesk/client.py
import asyncio
import logging
import time
from typing import Any, Dict, Optional

import aiohttp
from .config import settings

log = logging.getLogger(__name__)

class IGClient:
    """Thin wrapper around IG's REST API – handles auth & simple GET/POST."""

    # ------------------------------------------------------------------
    # End-points
    # ------------------------------------------------------------------
    DEMO_BASE = "https://demo-api.ig.com/gateway/deal"
    LIVE_BASE = "https://api.ig.com/gateway/deal"

    # Lightstreamer hosts (used by the strategy)
    DEMO_LS = "https://demo-apd.marketdatasystems.com"
    LIVE_LS = "https://push.lightstreamer.com"

    def __init__(self):
        # Choose the correct base URL for the selected environment
        self.base_url = self.DEMO_BASE if settings.environment == "DEMO" else self.LIVE_BASE
        self.ls_url   = self.DEMO_LS   if settings.environment == "DEMO" else self.LIVE_LS

        # VERSION 2 returns CST/X-SECURITY-TOKEN (works with Lightstreamer)
        # VERSION 3 returns OAuth tokens (doesn't work with Lightstreamer)
        # For demo with Lightstreamer support, use VERSION 2
        api_version = "2" if settings.environment == "DEMO" else "3"
        
        # Store headers for session creation
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "VERSION": api_version,
            "X-IG-API-KEY": settings.ig_api_key,
        }

        # OAuth token management
        self.uses_oauth = False
        self.oauth_access_token: Optional[str] = None
        self.oauth_refresh_token: Optional[str] = None
        self.oauth_expires_at: float = 0  # Unix timestamp
        self.account_id: Optional[str] = None
        self.client_id: Optional[str] = None
        
        # Lightstreamer authentication tokens (different from OAuth!)
        self.ls_cst: Optional[str] = None
        self.ls_xst: Optional[str] = None
        
        # Rate limiting and concurrency control
        self.last_auth_attempt: float = 0
        self.min_auth_interval: float = 5.0  # Minimum 5 seconds between auth attempts
        self._auth_lock = asyncio.Lock()  # Prevent concurrent authentication
        self._session: Optional[aiohttp.ClientSession] = None
        
        # Note: We don't authenticate here anymore - moved to async context

    async def __aenter__(self):
        """Async context manager entry."""
        await self.start()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
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
    # Authentication – handles both OAuth (demo) and CST/token (live)
    # ------------------------------------------------------------------
    async def _authenticate(self) -> None:
        """
        Calls IG's `/session` endpoint and extracts authentication credentials.
        
        Demo API uses OAuth tokens (access_token in JSON body).
        Live API uses CST and X-SECURITY-TOKEN (in headers/cookies).
        """
        async with self._auth_lock:
            # Rate limit protection - don't hammer the auth endpoint
            now = time.time()
            time_since_last = now - self.last_auth_attempt
            if time_since_last < self.min_auth_interval:
                wait_time = self.min_auth_interval - time_since_last
                log.debug("Rate limiting: waiting %.1f seconds before re-authentication", wait_time)
                await asyncio.sleep(wait_time)
            
            self.last_auth_attempt = time.time()
            
            url = f"{self.base_url}/session"
            payload = {
                "identifier": settings.ig_username,
                "password": settings.ig_password,
            }

            log.debug(
                "POST %s – authenticating with IG (environment=%s)",
                url,
                settings.environment,
            )
            
            if not self._session:
                self._session = aiohttp.ClientSession(headers=self.headers)
            
            try:
                async with self._session.post(url, json=payload) as resp:
                    # --------------------------------------------------------------
                    # 1️⃣ Non-200 responses – surface the body for debugging.
                    # --------------------------------------------------------------
                    if resp.status != 200:
                        try:
                            body = await resp.json()
                        except Exception:
                            body = await resp.text()
                        
                        # Check for rate limiting
                        if resp.status == 403 and isinstance(body, dict):
                            if body.get("errorCode") == "error.public-api.exceeded-api-key-allowance":
                                log.error(
                                    "IG API rate limit exceeded. You need to wait before making more requests. "
                                    "Consider using Lightstreamer for real-time data instead of polling."
                                )
                                raise RuntimeError(
                                    "IG API rate limit exceeded. Wait a few minutes before retrying, "
                                    "or switch to Lightstreamer for real-time data."
                                )
                        
                        log.error(
                            "IG authentication failed (HTTP %s). Body: %s",
                            resp.status,
                            body,
                        )
                        raise RuntimeError(
                            f"IG authentication failed – HTTP {resp.status}. "
                            "Check credentials, API key, and that you are using the correct endpoint."
                        )

                    # --------------------------------------------------------------
                    # 2️⃣ Parse the JSON payload
                    # --------------------------------------------------------------
                    try:
                        json_body = await resp.json()
                    except Exception:
                        json_body = {}
                        log.debug("Response body could not be parsed as JSON.")

                    # --------------------------------------------------------------
                    # 3️⃣ Check for OAuth token (VERSION 3 - doesn't work with Lightstreamer)
                    # --------------------------------------------------------------
                    oauth_token = json_body.get("oauthToken", {})
                    if oauth_token and oauth_token.get("access_token"):
                        await self._store_oauth_token(oauth_token, json_body.get("accountId", ""), json_body.get("clientId", ""))
                        expires_in = oauth_token.get("expires_in", "unknown")
                        log.warning(
                            "Authenticated with OAuth (VERSION 3) - Lightstreamer streaming NOT available. "
                            "OAuth tokens don't work with Lightstreamer. System will use REST polling."
                        )
                        return

                    # --------------------------------------------------------------
                    # 4️⃣ Otherwise look for CST/X-SECURITY-TOKEN (VERSION 2 or live)
                    # --------------------------------------------------------------
                    def _find_token(name: str, json_key: str) -> Optional[str]:
                        """
                        `name`      – the exact header/cookie name (e.g. "CST").
                        `json_key`  – the key used in the JSON payload (e.g. "cst").
                        Returns the token string or None.
                        """
                        # a) Header (live endpoint)
                        token = resp.headers.get(name)
                        if token:
                            log.info("%s obtained from response header.", name)
                            return token
                        
                        # b) JSON payload
                        token = json_body.get(json_key) or json_body.get(json_key.upper())
                        if token:
                            log.info("%s obtained from JSON payload.", name)
                            return token

                        return None

                    cst = _find_token("CST", "cst")
                    x_sec = _find_token("X-SECURITY-TOKEN", "x-security-token")

                    # --------------------------------------------------------------
                    # 5️⃣ Validate – if either is missing, raise a clear error.
                    # --------------------------------------------------------------
                    missing = []
                    if not cst:
                        missing.append("CST")
                    if not x_sec:
                        missing.append("X-SECURITY-TOKEN")

                    if missing:
                        log.error(
                            "Authentication tokens missing after all extraction attempts. "
                            "Full response headers: %s\nJSON body: %s",
                            dict(resp.headers),
                            json_body,
                        )
                        raise RuntimeError(
                            f"{' and '.join(missing)} not found in IG authentication response. "
                            "Common causes: wrong endpoint, mismatched demo/live credentials, "
                            "or a temporary IG service change."
                        )

                    # --------------------------------------------------------------
                    # 6️⃣ Store the tokens in the session for all subsequent calls.
                    # --------------------------------------------------------------
                    # Update session headers
                    self.headers.update({"CST": cst, "X-SECURITY-TOKEN": x_sec})
                    # Also update the current session if it exists
                    if self._session:
                        self._session.headers.update({"CST": cst, "X-SECURITY-TOKEN": x_sec})
                    
                    self.ls_cst = cst
                    self.ls_xst = x_sec
                    self.client_id = json_body.get("clientId")
                    self.account_id = json_body.get("currentAccountId") or json_body.get("accountId")
                    self.uses_oauth = False
                    log.info(
                        "Authenticated to IG (%s) – CST and X-SECURITY-TOKEN stored (Lightstreamer available)",
                        settings.environment,
                    )
            except aiohttp.ClientError as e:
                log.error("Network error during authentication: %s", e)
                raise RuntimeError(f"Network error during authentication: {e}")

    # ------------------------------------------------------------------
    # OAuth token management
    # ------------------------------------------------------------------
    async def _store_oauth_token(self, oauth_token: Dict[str, Any], account_id: str, client_id: str) -> None:
        """Store OAuth credentials and calculate expiry time."""
        self.oauth_access_token = oauth_token["access_token"]
        self.oauth_refresh_token = oauth_token.get("refresh_token")
        self.account_id = account_id
        self.client_id = client_id
        
        # Calculate when token expires (with 5 second buffer for safety)
        expires_in = int(oauth_token.get("expires_in", 30))
        self.oauth_expires_at = time.time() + expires_in - 5
        
        # Update session headers
        self.headers.update({
            "Authorization": f"Bearer {self.oauth_access_token}",
            "IG-ACCOUNT-ID": account_id
        })
        if self._session:
            self._session.headers.update({
                "Authorization": f"Bearer {self.oauth_access_token}",
                "IG-ACCOUNT-ID": account_id
            })
        self.uses_oauth = True

    def _is_token_valid(self) -> bool:
        """Check if the current token is still valid."""
        if not self.uses_oauth:
            return True  # CST/token auth doesn't expire as quickly
        
        return time.time() < self.oauth_expires_at

    # ------------------------------------------------------------------
    # Generic request helper – only re-authenticates when necessary.
    # ------------------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        
        # Ensure we have a session
        if not self._session:
            self._session = aiohttp.ClientSession(headers=self.headers)
        
        # Only check token expiry if we haven't already authenticated very recently
        if self.uses_oauth:
            time_since_auth = time.time() - self.last_auth_attempt
            if time_since_auth > 25 and not self._is_token_valid():
                log.debug("OAuth token expired – re-authenticating")
                await self._authenticate()
        
        try:
            async with self._session.request(method, url, **kwargs) as resp:
                # If we get 401/403, try re-authenticating ONCE
                if resp.status in (401, 403):
                    # Check if this is a rate limit error
                    try:
                        body = await resp.json()
                        if body.get("errorCode") == "error.public-api.exceeded-api-key-allowance":
                            log.error("API rate limit exceeded - cannot retry")
                            raise RuntimeError(
                                "IG API rate limit exceeded. Wait before retrying or use Lightstreamer."
                            )
                    except (ValueError, KeyError):
                        pass
                    
                    log.warning("Auth failed (HTTP %s) – attempting re-authentication", resp.status)
                    await self._authenticate()
                    # Retry the request with new credentials
                    async with self._session.request(method, url, **kwargs) as retry_resp:
                        retry_resp.raise_for_status()
                        return await retry_resp.json()
                
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as e:
            log.error("Request failed: %s %s - %s", method, url, e)
            raise

    # ------------------------------------------------------------------
    # Public helpers used by the strategy (and later by real trading code)
    # ------------------------------------------------------------------
    async def get_market_snapshot(self, epic: str) -> Dict[str, Any]:
        """Return the latest market snapshot for the given EPIC."""
        return await self._request("GET", f"/markets/{epic}")

    async def get_price_ticks(self, epic: str) -> Dict[str, Any]:
        """Convenient shortcut to the "prices" endpoint – useful for polling."""
        return await self._request("GET", f"/prices/{epic}")

    # ------------------------------------------------------------------
    # Example order-placement helper (not exercised by the current tests)
    # ------------------------------------------------------------------
    async def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        currency: str = "USD",
        force_open: bool = True,
    ) -> Dict[str, Any]:
        """Submit a simple market order."""
        order = {
            "epic": epic,
            "direction": direction.upper(),
            "size": size,
            "orderType": "MARKET",
            "currencyCode": currency,
            "forceOpen": force_open,
        }
        return await self._request("POST", "/positions/otc", json=order)
