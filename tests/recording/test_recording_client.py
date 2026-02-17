"""Tests for tradedesk.recording.client – RecordingClient wrapper."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.recording.client import RecordingClient
from tradedesk.recording.ledger import TradeLedger
from tradedesk.recording.types import RecordingMode


@pytest.fixture
def ledger():
    return TradeLedger(mode=RecordingMode.BACKTEST)


@pytest.fixture
def mock_inner():
    inner = MagicMock()
    inner._current_timestamp = "2025-01-15T12:00:00Z"
    inner.place_market_order = AsyncMock(return_value={"price": 150.0})
    inner.place_market_order_confirmed = AsyncMock(return_value={"price": 151.0})
    inner.some_other_method = MagicMock(return_value="delegated")
    return inner


@pytest.fixture
def client(mock_inner, ledger):
    return RecordingClient(mock_inner, ledger=ledger)


class TestRecordingClient:
    def test_getattr_delegates(self, client, mock_inner):
        assert client.some_other_method() == "delegated"
        mock_inner.some_other_method.assert_called_once()

    @pytest.mark.asyncio
    async def test_place_market_order_records_trade(self, client, ledger, mock_inner):
        resp = await client.place_market_order(
            instrument="USDJPY", direction="BUY", size=1.0
        )
        assert resp == {"price": 150.0}
        assert len(ledger.trades) == 1
        assert ledger.trades[0].instrument == "USDJPY"
        assert ledger.trades[0].direction == "BUY"
        assert ledger.trades[0].price == 150.0
        assert ledger.trades[0].size == 1.0

    @pytest.mark.asyncio
    async def test_place_market_order_confirmed_records_trade(self, client, ledger):
        resp = await client.place_market_order_confirmed(
            instrument="GBPUSD", direction="SELL", size=2.0
        )
        assert resp == {"price": 151.0}
        assert len(ledger.trades) == 1
        assert ledger.trades[0].instrument == "GBPUSD"
        assert ledger.trades[0].direction == "SELL"
        assert ledger.trades[0].price == 151.0

    @pytest.mark.asyncio
    async def test_record_trade_uses_mark_price_when_no_price(self, ledger):
        inner = MagicMock()
        inner._current_timestamp = "2025-01-15T12:00:00Z"
        inner.place_market_order = AsyncMock(return_value={})  # no price
        inner.get_mark_price = MagicMock(return_value=155.0)

        rc = RecordingClient(inner, ledger=ledger)
        await rc.place_market_order(instrument="USDJPY", direction="BUY", size=1.0)
        assert ledger.trades[0].price == 155.0

    @pytest.mark.asyncio
    async def test_record_trade_zero_price_when_no_mark(self, ledger):
        inner = MagicMock()
        inner._current_timestamp = "2025-01-15T12:00:00Z"
        inner.place_market_order = AsyncMock(return_value={})  # no price
        # No get_mark_price attribute
        del inner.get_mark_price

        rc = RecordingClient(inner, ledger=ledger)
        await rc.place_market_order(instrument="USDJPY", direction="BUY", size=1.0)
        assert ledger.trades[0].price == 0.0

    def test_current_timestamp_from_inner(self, client):
        assert client._current_timestamp() == "2025-01-15T12:00:00Z"

    def test_current_timestamp_fallback_to_now(self, ledger):
        inner = MagicMock()
        inner._current_timestamp = ""  # empty string
        rc = RecordingClient(inner, ledger=ledger)
        ts = rc._current_timestamp()
        # Should be a valid ISO string (not empty)
        assert len(ts) > 0
        assert "T" in ts

    def test_current_timestamp_fallback_no_attr(self, ledger):
        inner = MagicMock(spec=[])  # no _current_timestamp attribute
        rc = RecordingClient(inner, ledger=ledger)
        ts = rc._current_timestamp()
        assert len(ts) > 0
