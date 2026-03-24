# tradedesk/execution/ig/auth.py
"""IG API authentication and session lifecycle."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import aiohttp

if TYPE_CHECKING:
    from .client import IGClient
    from .settings import Settings

log = logging.getLogger(__name__)


class IGAuthManager:
    """Manages IG API session authentication and token lifecycle."""

    def __init__(self, client: IGClient, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._auth_lock: asyncio.Lock = asyncio.Lock()
        self.last_auth_attempt: float = 0
        self.min_auth_interval: float = 5.0
        self.uses_oauth: bool = False
        self.oauth_access_token: str | None = None
        self.oauth_refresh_token: str | None = None
        self.oauth_expires_at: float = 0
        self.account_id: str | None = None
        self.client_id: str | None = None
        self.ls_cst: str | None = None
        self.ls_xst: str | None = None

    def is_token_valid(self) -> bool:
        """Return True if the current session token is still valid."""
        if not self.uses_oauth:
            return True
        return time.time() < self.oauth_expires_at

    async def authenticate(self) -> None:
        """Rate-limit, execute auth request, dispatch to version handler."""
        async with self._auth_lock:
            await self._enforce_rate_limit()
            resp_headers, resp_body = await self._perform_auth_request()
            if self._client.api_version == "3":
                await self._handle_v3_auth(resp_body)
            else:
                self._handle_v2_auth(resp_headers, resp_body)

    async def _enforce_rate_limit(self) -> None:
        now = time.time()
        elapsed = now - self.last_auth_attempt
        if elapsed < self.min_auth_interval:
            wait = self.min_auth_interval - elapsed
            log.debug("Rate limiting: waiting %.1f seconds before re-authentication", wait)
            await asyncio.sleep(wait)
        self.last_auth_attempt = time.time()

    async def _perform_auth_request(self) -> tuple[dict[str, Any], dict[str, Any]]:
        url = f"{self._client.base_url}/session"
        payload = {
            "identifier": self._settings.ig_username,
            "password": self._settings.ig_password,
        }
        log.debug("POST %s – authenticating with IG (v%s)", url, self._client.api_version)

        if not self._client._session:
            self._client._session = aiohttp.ClientSession(headers=self._client.headers)

        try:
            async with self._client._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    await self._handle_auth_error(resp)
                try:
                    body = await resp.json()
                except Exception:
                    body = {}
                return dict(resp.headers), body
        except aiohttp.ClientError as e:
            log.error("Network error during authentication: %s", e)
            raise RuntimeError(f"Network error during authentication: {e}")

    async def _handle_auth_error(self, resp: aiohttp.ClientResponse) -> None:
        try:
            body = await resp.json()
        except Exception:
            body = await resp.text()

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

    def _handle_v2_auth(self, headers: dict[str, Any], body: dict[str, Any]) -> None:
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

        self._client._apply_session_headers(
            {
                "CST": cst,
                "X-SECURITY-TOKEN": x_sec,
                "IG-ACCOUNT-ID": self.account_id,
            }
        )
        log.info("Authenticated (V2) – Streaming enabled.")

    async def _handle_v3_auth(self, body: dict[str, Any]) -> None:
        oauth_token = body.get("oauthToken") or {}
        access_token = oauth_token.get("access_token")

        if not access_token:
            log.error("Missing OAuth token in V3 response: %s", body)
            raise RuntimeError("OAuth access_token not found in IG response.")

        await self._store_oauth_token(
            oauth_token, body.get("accountId", ""), body.get("clientId", "")
        )
        log.warning(
            "Authenticated (V3 OAuth) – Streaming NOT available. System will use REST polling."
        )

    async def _store_oauth_token(
        self, oauth_token: dict[str, Any], account_id: str, client_id: str
    ) -> None:
        self.oauth_access_token = oauth_token["access_token"]
        self.oauth_refresh_token = oauth_token.get("refresh_token")
        self.account_id = account_id
        self.client_id = client_id

        expires_in = int(oauth_token.get("expires_in", 30))
        self.oauth_expires_at = time.time() + expires_in - 5

        self._client._apply_session_headers(
            {
                "Authorization": f"Bearer {self.oauth_access_token}",
                "IG-ACCOUNT-ID": account_id,
            }
        )
        self.uses_oauth = True
