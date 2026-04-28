"""Tests for the event-driven order execution pipeline."""

from unittest.mock import AsyncMock

import pytest

from tradedesk.execution.order_handler import OrderExecutionHandler, request_order
from tradedesk.recording.events import PositionOpenedEvent
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
        strategy="",
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


def _snapshot(bid: float, offer: float, scaling_factor: float = 1) -> dict:
    return {
        "snapshot": {"bid": bid, "offer": offer},
        "instrument": {"scalingFactor": scaling_factor},
    }


@pytest.mark.asyncio
async def test_spread_gate_blocks_eurusd_wide_spread():
    """EURUSD blocked when IG-scaled spread exceeds pip limit (realistic fixture)."""
    client = AsyncMock()
    # IG returns EURUSD ×10000: bid=11710.0, offer=11725.0 → 15 pip spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=11710.0, offer=11725.0, scaling_factor=10000
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    # 2.0 pips × 0.0001 pip_divisor = 0.0002 decimal threshold
    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0002})

    result = await request_order(
        OrderRequest(instrument="CS.D.EURUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error
    assert "CS.D.EURUSD.TODAY.IP" in result.error
    client.place_market_order_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_allows_eurusd_normal_spread():
    """EURUSD passes when IG-scaled spread is below pip limit (realistic fixture)."""
    client = AsyncMock()
    # IG returns EURUSD ×10000: bid=11715.5, offer=11716.4 → 0.9 pip spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=11715.5, offer=11716.4, scaling_factor=10000
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 11716.0}

    # 2.0 pips × 0.0001 pip_divisor = 0.0002 decimal threshold
    OrderExecutionHandler(client, spread_limits={"CS.D.EURUSD.TODAY.IP": 0.0002})

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
async def test_spread_gate_blocks_gbpusd_wide_spread():
    """GBPUSD blocked with IG-scaled wide spread."""
    client = AsyncMock()
    # IG returns GBPUSD ×10000: bid=12600.0, offer=12610.0 → 10 pip spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=12600.0, offer=12610.0, scaling_factor=10000
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    # 3.0 pips × 0.0001 pip_divisor = 0.0003 decimal threshold
    OrderExecutionHandler(client, spread_limits={"CS.D.GBPUSD.TODAY.IP": 0.0003})

    result = await request_order(
        OrderRequest(instrument="CS.D.GBPUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error
    client.place_market_order_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_allows_gbpusd_normal_spread():
    """GBPUSD passes with IG-scaled normal spread."""
    client = AsyncMock()
    # IG returns GBPUSD ×10000: bid=12605.0, offer=12606.2 → 1.2 pip spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=12605.0, offer=12606.2, scaling_factor=10000
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 12606.0}

    OrderExecutionHandler(client, spread_limits={"CS.D.GBPUSD.TODAY.IP": 0.0003})

    result = await request_order(
        OrderRequest(instrument="CS.D.GBPUSD.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.place_market_order_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_spread_gate_blocks_usdjpy_wide_spread():
    """USDJPY blocked with IG-scaled wide spread (×100)."""
    client = AsyncMock()
    # IG returns USDJPY ×100: bid=15580.0, offer=15590.0 → 10 pip spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=15580.0, offer=15590.0, scaling_factor=100
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    # 2.5 pips × 0.01 pip_divisor = 0.025 decimal threshold
    OrderExecutionHandler(client, spread_limits={"CS.D.USDJPY.TODAY.IP": 0.025})

    result = await request_order(
        OrderRequest(instrument="CS.D.USDJPY.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error
    client.place_market_order_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_allows_usdjpy_normal_spread():
    """USDJPY passes with IG-scaled normal spread (×100)."""
    client = AsyncMock()
    # IG returns USDJPY ×100: bid=15582.5, offer=15583.2 → 0.7 pip spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=15582.5, offer=15583.2, scaling_factor=100
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 15583.0}

    OrderExecutionHandler(client, spread_limits={"CS.D.USDJPY.TODAY.IP": 0.025})

    result = await request_order(
        OrderRequest(instrument="CS.D.USDJPY.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.place_market_order_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_spread_gate_blocks_dax_wide_spread():
    """DAX blocked with unscaled wide spread (scalingFactor=1)."""
    client = AsyncMock()
    # DAX: unscaled, bid=24120.0, offer=24126.0 → 6 point spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=24120.0, offer=24126.0, scaling_factor=1
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    # 4.0 points × 1.0 pip_divisor = 4.0 decimal threshold
    OrderExecutionHandler(client, spread_limits={"IX.D.DAX.DAILY.IP": 4.0})

    result = await request_order(
        OrderRequest(instrument="IX.D.DAX.DAILY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error
    client.place_market_order_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_spread_gate_allows_dax_normal_spread():
    """DAX passes with unscaled normal spread (regression check)."""
    client = AsyncMock()
    # DAX: unscaled, bid=24120.0, offer=24121.5 → 1.5 point spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=24120.0, offer=24121.5, scaling_factor=1
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 24121.0}

    OrderExecutionHandler(client, spread_limits={"IX.D.DAX.DAILY.IP": 4.0})

    result = await request_order(
        OrderRequest(instrument="IX.D.DAX.DAILY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.place_market_order_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_spread_gate_allows_xauusd_normal_spread():
    """XAUUSD (gold) passes with unscaled normal spread (regression check)."""
    client = AsyncMock()
    # Gold: unscaled, bid=2362.45, offer=2362.95 → 0.50 raw spread
    client.get_market_snapshot.return_value = _snapshot(
        bid=2362.45, offer=2362.95, scaling_factor=1
    )
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 2362.70}

    # 100 pips × 0.01 pip_divisor = 1.0 decimal threshold
    OrderExecutionHandler(client, spread_limits={"CS.D.USCGC.TODAY.IP": 1.0})

    result = await request_order(
        OrderRequest(instrument="CS.D.USCGC.TODAY.IP", direction="BUY", size=1.0)
    )

    assert result.success is True
    client.place_market_order_confirmed.assert_awaited_once()


@pytest.mark.asyncio
async def test_spread_gate_missing_scaling_factor_defaults_to_one():
    """When instrument metadata lacks scalingFactor, default to 1 (no scaling)."""
    client = AsyncMock()
    # Snapshot without instrument metadata at all
    client.get_market_snapshot.return_value = {
        "snapshot": {"bid": 24120.0, "offer": 24126.0},
    }
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    OrderExecutionHandler(client, spread_limits={"IX.D.DAX.DAILY.IP": 4.0})

    result = await request_order(
        OrderRequest(instrument="IX.D.DAX.DAILY.IP", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "Spread gate blocked" in result.error


# ---------------------------------------------------------------------------
# Order gate tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_gate_blocks_when_returning_error():
    """Order is blocked when order_gate returns an error string."""
    client = AsyncMock()
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 100.0}

    OrderExecutionHandler(client, order_gate=lambda: "paused")

    result = await request_order(
        OrderRequest(instrument="TEST", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "paused" in result.error
    client.place_market_order_confirmed.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_gate_allows_when_returning_none():
    """Order proceeds when order_gate returns None."""
    client = AsyncMock()
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {"level": 100.0}

    OrderExecutionHandler(client, order_gate=lambda: None)

    result = await request_order(
        OrderRequest(instrument="TEST", direction="BUY", size=1.0)
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_order_gate_checked_before_spread_gate():
    """Order gate is checked before the spread gate (no snapshot fetch)."""
    client = AsyncMock()
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)

    OrderExecutionHandler(
        client,
        spread_limits={"TEST": 0.001},
        order_gate=lambda: "blocked by gate",
    )

    result = await request_order(
        OrderRequest(instrument="TEST", direction="BUY", size=1.0)
    )

    assert result.success is False
    assert "blocked by gate" in result.error
    # Spread gate should not be reached
    client.get_market_snapshot.assert_not_awaited()


# ---------------------------------------------------------------------------
# PositionOpenedEvent emission tests
# ---------------------------------------------------------------------------


def _make_client(publishes_position_events: bool = False) -> AsyncMock:
    """Create a mock client with the publishes_position_events attribute."""
    client = AsyncMock()
    client.publishes_position_events = publishes_position_events
    client.quantise_size = AsyncMock(side_effect=lambda inst, size: size)
    client.place_market_order_confirmed.return_value = {
        "level": 105.50,
        "size": 1.0,
        "dealId": "DEAL-123",
    }
    return client


@pytest.mark.asyncio
async def test_position_opened_event_emitted_for_ig_fill():
    """PositionOpenedEvent is published when client does not emit its own."""
    client = _make_client(publishes_position_events=False)
    OrderExecutionHandler(client)

    published_events: list[PositionOpenedEvent] = []

    from tradedesk.events import get_dispatcher

    async def capture(event: PositionOpenedEvent) -> None:
        published_events.append(event)

    get_dispatcher().subscribe(PositionOpenedEvent, capture)

    result = await request_order(
        OrderRequest(
            instrument="CS.D.EURUSD.TODAY.IP",
            direction="BUY",
            size=1.0,
            force_open=True,
            strategy="test_strat",
        )
    )

    assert result.success is True
    assert len(published_events) == 1
    evt = published_events[0]
    assert evt.instrument == "CS.D.EURUSD.TODAY.IP"
    assert evt.direction == "BUY"
    assert evt.size == 1.0
    assert evt.entry_price == 105.50
    assert evt.strategy == "test_strat"
    assert evt.position_id == "DEAL-123"


@pytest.mark.asyncio
async def test_no_position_event_when_client_publishes_own():
    """No extra PositionOpenedEvent when client already emits them."""
    client = _make_client(publishes_position_events=True)
    OrderExecutionHandler(client)

    published_events: list[PositionOpenedEvent] = []

    from tradedesk.events import get_dispatcher

    async def capture(event: PositionOpenedEvent) -> None:
        published_events.append(event)

    get_dispatcher().subscribe(PositionOpenedEvent, capture)

    result = await request_order(
        OrderRequest(
            instrument="TEST",
            direction="BUY",
            size=1.0,
            force_open=True,
        )
    )

    assert result.success is True
    assert len(published_events) == 0


@pytest.mark.asyncio
async def test_no_position_event_when_force_open_false():
    """No PositionOpenedEvent when force_open is False (position close)."""
    client = _make_client(publishes_position_events=False)
    OrderExecutionHandler(client)

    published_events: list[PositionOpenedEvent] = []

    from tradedesk.events import get_dispatcher

    async def capture(event: PositionOpenedEvent) -> None:
        published_events.append(event)

    get_dispatcher().subscribe(PositionOpenedEvent, capture)

    result = await request_order(
        OrderRequest(
            instrument="TEST",
            direction="SELL",
            size=1.0,
            force_open=False,
        )
    )

    assert result.success is True
    assert len(published_events) == 0
