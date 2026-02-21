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

    result = await request_order(
        OrderRequest(instrument="TEST", direction="BUY", size=1.0)
    )

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

    result = await request_order(
        OrderRequest(instrument="TEST", direction="SELL", size=1.0)
    )

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

    result = await request_order(
        OrderRequest(instrument="TEST", direction="BUY", size=1.0)
    )

    assert result.fill_price == 99.0
