"""Tests for IGClient.get_positions and get_account_balance."""

import pytest

from tradedesk.execution.ig.client import IGClient


@pytest.fixture
def ig_client(monkeypatch):
    monkeypatch.setenv("IG_API_KEY", "test")
    monkeypatch.setenv("IG_USERNAME", "test")
    monkeypatch.setenv("IG_PASSWORD", "test")
    monkeypatch.setenv("IG_ENVIRONMENT", "DEMO")
    client = IGClient()
    client.account_id = "ABC123"
    return client


@pytest.mark.asyncio
async def test_get_positions(ig_client, monkeypatch):
    async def mock_request(method, path, **kw):
        assert method == "GET"
        assert path == "/positions"
        return {
            "positions": [
                {
                    "market": {"epic": "CS.D.USDJPY.TODAY.IP"},
                    "position": {
                        "direction": "BUY",
                        "size": 0.5,
                        "level": 150.234,
                        "dealId": "DEAL1",
                        "currency": "GBP",
                        "createdDateUTC": "2026-01-01T12:00:00",
                    },
                },
                {
                    "market": {"epic": "CS.D.GBPUSD.TODAY.IP"},
                    "position": {
                        "direction": "SELL",
                        "size": 1.0,
                        "level": 1.2650,
                        "dealId": "DEAL2",
                        "currency": "GBP",
                        "createdDateUTC": "2026-01-02T08:30:00",
                    },
                },
            ]
        }

    monkeypatch.setattr(ig_client, "_request", mock_request)
    positions = await ig_client.get_positions()

    assert len(positions) == 2

    assert positions[0].instrument == "CS.D.USDJPY.TODAY.IP"
    assert positions[0].direction == "BUY"
    assert positions[0].size == 0.5
    assert positions[0].entry_price == 150.234
    assert positions[0].deal_id == "DEAL1"

    assert positions[1].instrument == "CS.D.GBPUSD.TODAY.IP"
    assert positions[1].direction == "SELL"
    assert positions[1].size == 1.0


@pytest.mark.asyncio
async def test_get_positions_empty(ig_client, monkeypatch):
    async def mock_request(method, path, **kw):
        return {"positions": []}

    monkeypatch.setattr(ig_client, "_request", mock_request)
    positions = await ig_client.get_positions()
    assert positions == []


@pytest.mark.asyncio
async def test_get_account_balance(ig_client, monkeypatch):
    async def mock_get_accounts():
        return {
            "accounts": [
                {
                    "accountId": "ABC123",
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

    monkeypatch.setattr(ig_client, "_get_accounts", mock_get_accounts)
    bal = await ig_client.get_account_balance()

    assert bal.balance == 10000.0
    assert bal.deposit == 500.0
    assert bal.available == 9500.0
    assert bal.profit_loss == 150.0
    assert bal.currency == "GBP"


@pytest.mark.asyncio
async def test_get_account_balance_account_not_found(ig_client, monkeypatch):
    async def mock_get_accounts():
        return {"accounts": [{"accountId": "OTHER", "balance": {}}]}

    monkeypatch.setattr(ig_client, "_get_accounts", mock_get_accounts)
    with pytest.raises(RuntimeError, match="not found"):
        await ig_client.get_account_balance()
