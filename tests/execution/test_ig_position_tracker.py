"""Unit tests for IGPositionTracker."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.execution.ig.positions import IGPositionTracker


def _make_client(account_id: str | None = "ACC123") -> MagicMock:
    client = MagicMock()
    client.account_id = account_id
    client._request = AsyncMock()
    client._get_accounts = AsyncMock()
    return client


class TestGetPositions:
    async def test_parses_positions_correctly(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            return_value={
                "positions": [
                    {
                        "market": {"epic": "CS.D.USDJPY.TODAY.IP"},
                        "position": {
                            "direction": "BUY",
                            "size": 0.5,
                            "level": 150.0,
                            "dealId": "D1",
                            "currency": "GBP",
                            "createdDateUTC": "2026-01-01T12:00:00",
                        },
                    }
                ]
            }
        )
        tracker = IGPositionTracker(client)
        positions = await tracker.get_positions()

        assert len(positions) == 1
        assert positions[0].instrument == "CS.D.USDJPY.TODAY.IP"
        assert positions[0].direction == "BUY"
        assert positions[0].size == 0.5
        assert positions[0].entry_price == 150.0
        assert positions[0].deal_id == "D1"

    async def test_returns_empty_list_when_no_positions(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"positions": []})
        tracker = IGPositionTracker(client)

        positions = await tracker.get_positions()
        assert positions == []

    async def test_uses_api_version_2(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"positions": []})
        tracker = IGPositionTracker(client)

        await tracker.get_positions()
        client._request.assert_awaited_once_with("GET", "/positions", api_version="2")


class TestGetAccountBalance:
    async def test_returns_balance_for_matching_account(self) -> None:
        client = _make_client("ACC123")
        client._get_accounts = AsyncMock(
            return_value={
                "accounts": [
                    {
                        "accountId": "ACC123",
                        "balance": {
                            "balance": 10000.0,
                            "deposit": 500.0,
                            "available": 9500.0,
                            "profitLoss": 150.0,
                        },
                        "currency": "GBP",
                    }
                ]
            }
        )
        tracker = IGPositionTracker(client)
        bal = await tracker.get_account_balance()

        assert bal.balance == 10000.0
        assert bal.deposit == 500.0
        assert bal.available == 9500.0
        assert bal.profit_loss == 150.0
        assert bal.currency == "GBP"

    async def test_raises_when_account_not_found(self) -> None:
        client = _make_client("ACC123")
        client._get_accounts = AsyncMock(
            return_value={"accounts": [{"accountId": "OTHER"}]}
        )
        tracker = IGPositionTracker(client)

        with pytest.raises(RuntimeError, match="not found"):
            await tracker.get_account_balance()
