# tradedesk/execution/ig/positions.py
"""IG live position and account balance tracking."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tradedesk.execution.broker import AccountBalance, BrokerPosition

if TYPE_CHECKING:
    from .client import IGClient

log = logging.getLogger(__name__)


class IGPositionTracker:
    """Fetches live positions and account balance from IG REST API."""

    def __init__(self, client: IGClient) -> None:
        self._client = client

    async def get_positions(self) -> list[BrokerPosition]:
        """Fetch all open positions from IG REST API."""
        payload = await self._client._request("GET", "/positions", api_version="2")
        positions = payload.get("positions") or []
        result: list[BrokerPosition] = []
        for p in positions:
            market = p.get("market") or {}
            position = p.get("position") or {}
            result.append(
                BrokerPosition(
                    instrument=market.get("epic", ""),
                    direction=position.get("direction", ""),
                    size=float(position.get("size", 0)),
                    entry_price=float(position.get("level", 0)),
                    deal_id=position.get("dealId", ""),
                    currency=position.get("currency", ""),
                    created_at=position.get("createdDateUTC", ""),
                )
            )
        return result

    async def get_account_balance(self) -> AccountBalance:
        """Fetch current account balance from IG REST API."""
        payload = await self._client._get_accounts()
        accounts = payload.get("accounts") or []
        current = next(
            (a for a in accounts if a.get("accountId") == self._client.account_id),
            None,
        )
        if current is None:
            raise RuntimeError(
                f"Account {self._client.account_id} not found in /accounts response"
            )
        bal = current.get("balance") or {}
        return AccountBalance(
            balance=float(bal.get("balance", 0)),
            deposit=float(bal.get("deposit", 0)),
            available=float(bal.get("available", 0)),
            profit_loss=float(bal.get("profitLoss", 0)),
            currency=current.get("currency", ""),
        )
