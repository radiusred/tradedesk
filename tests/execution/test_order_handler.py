"""Tests for the event-driven order execution pipeline."""

from unittest.mock import AsyncMock

import pytest

from tradedesk.execution.order_handler import OrderExecutionHandler, request_order
from tradedesk.types import OrderRequest


@pytest.mark.asyncio
async def test_request_order_success():
    """request_order publishes event, handler executes, returns result."""
    client = AsyncMock()
    client.place_market_order_confirmed.return_value = {
        "level": 105.50,
        "size": 1.0,
    }
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    OrderExecutionHandler(client)

    result = await request_order(OrderRequest(instrument="TEST", direction="BUY", size=1.0))

    assert result.success is True
    assert result.fill_price == 105.50
    assert result.fill_size == 1.0
    client.place_market_order_confirmed.assert_awaited_once_with(
        instrument="TEST",
        direction="BUY",
        size=1.0,
        currency="USD",
        force_open=True,
        exit_reason="",
    )


@pytest.mark.asyncio
async def test_request_order_quantises_size():
    """Handler quantises size via client before placing."""
    client = AsyncMock()
    client.place_market_order_confirmed.return_value = {"price": 100.0}
    client.quantise_size = AsyncMock(return_value=0.5)
    OrderExecutionHandler(client)

    await request_order(OrderRequest(instrument="TEST", direction="BUY", size=0.47))

    client.quantise_size.assert_awaited_once_with("TEST", 0.47)
    client.place_market_order_confirmed.assert_awaited_once()
    call_kwargs = client.place_market_order_confirmed.call_args
    assert call_kwargs.kwargs["size"] == 0.5


@pytest.mark.asyncio
async def test_request_order_handles_rejection():
    """Handler returns failure when broker rejects."""
    client = AsyncMock()
    client.place_market_order_confirmed.side_effect = RuntimeError("rejected")
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    OrderExecutionHandler(client)

    result = await request_order(OrderRequest(instrument="TEST", direction="SELL", size=1.0))

    assert result.success is False
    assert "rejected" in result.error


@pytest.mark.asyncio
async def test_request_order_timeout_without_handler():
    """request_order returns failure when no handler is registered."""
    result = await request_order(
        OrderRequest(instrument="TEST", direction="BUY", size=1.0),
        timeout=0.1,
    )

    assert result.success is False
    assert "timed out" in result.error


@pytest.mark.asyncio
async def test_request_order_uses_fallback_price_field():
    """Handler extracts price from 'price' field when 'level' missing."""
    client = AsyncMock()
    client.place_market_order_confirmed.return_value = {"price": 99.0}
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    OrderExecutionHandler(client)

    result = await request_order(OrderRequest(instrument="TEST", direction="BUY", size=1.0))

    assert result.fill_price == 99.0


# ---------------------------------------------------------------------------
# Spread gate tests
# ---------------------------------------------------------------------------


def _snapshot(bid: float, offer: float) -> dict:
    return {"snapshot": {"bid": bid, "offer": offer}}


@pytest.mark.asyncio
async def test_spread_gate_blocks_when_spread_exceeds_limit():
    """Order is blocked when current spread exceeds configured limit."""
    client = AsyncMock()
    client.get_market_snapshot.return_value = _snapshot(bid=1.1000, offer=1.1010)
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 1.1005}

    # Max raw spread of 0.0005 (5 pips); current spread is 0.0010 (10 pips)
    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0005})

    result = await request_order(
        OrderRequest(instrument="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error
    assert "CS.D.EURUSD.TODAY.IP" in result.error
    client.place_market_order_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_allows_when_within_limit():
    """Order proceeds when spread is within the configured limit."""
    client = AsyncMock()
    client.get_market_snapshot.return_value = _snapshot(bid=1.1000, offer=1.1002)
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 1.1002}

    # Max raw spread of 0.0005; current spread is 0.0002 — within limit
    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0005})

    result = await request_order(
        OrderRequest(instrument="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.place_market_order_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_spread_gate_skips_unconfigured_instruments():
    """Instruments not in spread_limits are allowed through without check."""
    client = AsyncMock()
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 100.0}

    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0005})

    result = await request_order(
        OrderRequest(instrument="UNTRACKED", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.get_market_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_allows_on_snapshot_failure():
    """If snapshot fetch fails, the order is allowed through (fail-open)."""
    client = AsyncMock()
    client.get_market_snapshot.side_effect = RuntimeError("network error")
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 1.1000}

    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0005})

    result = await request_order(
        OrderRequest(instrument="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_spread_gate_allows_on_missing_bid_offer():
    """If snapshot lacks bid/offer, the order is allowed through."""
    client = AsyncMock()
    client.get_market_snapshot.return_value = {"snapshot": {}}
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 1.1000}

    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0005})

    result = await request_order(
        OrderRequest(instrument="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_spread_gate_disabled_when_no_limits():
    """No spread check when spread_limits is not provided."""
    client = AsyncMock()
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 100.0}

    OrderExecutionHandler(client)

    result = await request_order(
        OrderRequest(instrument="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.get_market_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_blocks_index_instrument():
    """Spread gate works for index instruments with point-based thresholds."""
    client = AsyncMock()
    # DAX: bid 18000, offer 18006 -> spread = 6 points
    client.get_market_snapshot.return_value = _snapshot(bid=18000.0, offer=18006.0)
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    # Max 4 points; current is 6 points -> block
    OrderExecutionHandler(client, spread_limits={"IX.D.DAX.DAILY.IP": 4.0})

    result = await request_order(
        OrderRequest(instrument="IX.D.DAX.DAILY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error
    client.place_market_order_confirmed.assert_not_awaited()
