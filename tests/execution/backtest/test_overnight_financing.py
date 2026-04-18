"""Tests for overnight financing and admin fee cost model."""

from __future__ import annotations

import pytest

from tradedesk.execution.backtest.client import BacktestClient, FinancingCosts
from tradedesk.types import Candle


def _make_candle(ts: str, close: float) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close)


def _client_with_prices(
    bid_close: float,
    ts: str = "2025-01-06T09:00:00Z",
) -> BacktestClient:
    candle = _make_candle(ts, bid_close)
    client = BacktestClient.from_history({("GOLD", "15MIN"): [candle]})
    client._mark_price["GOLD"] = bid_close
    client._set_current_timestamp(ts)
    client._started = True
    return client


@pytest.mark.asyncio
async def test_no_financing_config_preserves_behaviour() -> None:
    """Without financing config, no overnight costs are charged (non-breaking)."""
    client = _client_with_prices(3200.0, ts="2025-01-06T09:00:00Z")

    await client.place_market_order("GOLD", "BUY", size=1.0)
    assert client.realised_pnl == pytest.approx(0.0)

    # Advance to next day
    client._set_current_timestamp("2025-01-07T09:00:00Z")
    client._mark_price["GOLD"] = 3200.0

    # Close position
    client._set_current_timestamp("2025-01-07T10:00:00Z")
    await client.place_market_order("GOLD", "SELL", size=1.0)

    # PnL should be 0 (entered and exited at same price, no financing)
    assert client.realised_pnl == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_single_overnight_financing_charge() -> None:
    """A position held over one night incurs 1 day of financing."""
    client = _client_with_prices(3200.0, ts="2025-01-06T09:00:00Z")

    fc = FinancingCosts(admin_apr=0.025, finance_apr=0.05)
    client.set_financing_costs("GOLD", fc)

    # Monday entry
    await client.place_market_order("GOLD", "BUY", size=1.0)
    pnl_after_entry = client.realised_pnl

    # Cross to Tuesday — 1 financing day (Monday is weekday 0)
    client._set_current_timestamp("2025-01-07T09:00:00Z")
    client._mark_price["GOLD"] = 3200.0

    # Expected: notional=3200, rate=(0.025+0.05)/365, days=1
    expected_daily = 3200.0 * (0.025 + 0.05) / 365
    assert client.realised_pnl == pytest.approx(pnl_after_entry - expected_daily, rel=1e-6)


@pytest.mark.asyncio
async def test_multi_day_hold_accumulates() -> None:
    """A position held for 3 weekdays incurs 3 days of financing."""
    client = _client_with_prices(3200.0, ts="2025-01-06T09:00:00Z")

    fc = FinancingCosts(admin_apr=0.025, finance_apr=0.05)
    client.set_financing_costs("GOLD", fc)

    # Monday open
    await client.place_market_order("GOLD", "BUY", size=1.0)
    pnl_after_entry = client.realised_pnl

    # Jump to Thursday (3 overnights: Mon→Tue, Tue→Wed, Wed→Thu)
    client._set_current_timestamp("2025-01-09T09:00:00Z")
    client._mark_price["GOLD"] = 3200.0

    expected_3day = 3200.0 * (0.025 + 0.05) / 365 * 3
    assert client.realised_pnl == pytest.approx(pnl_after_entry - expected_3day, rel=1e-6)


@pytest.mark.asyncio
async def test_friday_multiplier_applies() -> None:
    """Friday overnight crossing charges 3× (configurable) for the weekend."""
    # 2025-01-10 is a Friday
    client = _client_with_prices(3200.0, ts="2025-01-10T09:00:00Z")

    fc = FinancingCosts(admin_apr=0.025, finance_apr=0.05, friday_multiplier=3)
    client.set_financing_costs("GOLD", fc)

    # Friday open
    await client.place_market_order("GOLD", "BUY", size=1.0)
    pnl_after_entry = client.realised_pnl

    # Cross to Monday (Fri→Mon = 1 overnight but with 3× multiplier)
    client._set_current_timestamp("2025-01-13T09:00:00Z")
    client._mark_price["GOLD"] = 3200.0

    # Fri crosses 3 days (Fri: 3× via multiplier), then Sat→Sun→Mon are 2 more crossings
    # Wait: from Fri to Mon there are 3 calendar days crossed:
    # Fri→Sat (Fri=weekday 4, gets 3× multiplier)
    # Sat→Sun (Sat=weekday 5, gets 1×)
    # Sun→Mon (Sun=weekday 6, gets 1×)
    # Total = 3 + 1 + 1 = 5 financing days
    expected_daily = 3200.0 * (0.025 + 0.05) / 365
    expected = expected_daily * 5
    assert client.realised_pnl == pytest.approx(pnl_after_entry - expected, rel=1e-6)


@pytest.mark.asyncio
async def test_friday_multiplier_custom_value() -> None:
    """Custom friday_multiplier is respected."""
    client = _client_with_prices(3200.0, ts="2025-01-10T09:00:00Z")

    fc = FinancingCosts(admin_apr=0.0, finance_apr=0.05, friday_multiplier=5)
    client.set_financing_costs("GOLD", fc)

    await client.place_market_order("GOLD", "BUY", size=1.0)

    # Fri→Sat only (1 day boundary, but Friday gets 5× multiplier)
    client._set_current_timestamp("2025-01-11T09:00:00Z")
    client._mark_price["GOLD"] = 3200.0

    expected = 3200.0 * 0.05 / 365 * 5
    assert client.realised_pnl == pytest.approx(-expected, rel=1e-6)


@pytest.mark.asyncio
async def test_financing_cost_flows_to_position_closed_event() -> None:
    """Financing and admin costs are included in PositionClosedEvent."""
    from tradedesk.events import get_dispatcher
    from tradedesk.recording import PositionClosedEvent

    dispatcher = get_dispatcher()
    closed_events: list[PositionClosedEvent] = []

    async def capture(ev: PositionClosedEvent) -> None:
        closed_events.append(ev)

    dispatcher.subscribe(PositionClosedEvent, capture)

    try:
        client = _client_with_prices(3200.0, ts="2025-01-06T09:00:00Z")
        fc = FinancingCosts(admin_apr=0.025, finance_apr=0.05)
        client.set_financing_costs("GOLD", fc)

        await client.place_market_order("GOLD", "BUY", size=1.0)

        # Hold over 2 nights (Mon→Wed)
        client._set_current_timestamp("2025-01-08T09:00:00Z")
        client._mark_price["GOLD"] = 3200.0
        await client.place_market_order("GOLD", "SELL", size=1.0)

        assert len(closed_events) == 1
        ev = closed_events[0]

        expected_admin = 3200.0 * 0.025 / 365 * 2
        expected_financing = 3200.0 * 0.05 / 365 * 2
        assert ev.admin_cost == pytest.approx(expected_admin, rel=1e-6)
        assert ev.financing_cost == pytest.approx(expected_financing, rel=1e-6)
    finally:
        dispatcher.unsubscribe(PositionClosedEvent, capture)


@pytest.mark.asyncio
async def test_financing_only_applies_to_configured_instruments() -> None:
    """Instruments without financing config are not charged."""
    client = _client_with_prices(3200.0, ts="2025-01-06T09:00:00Z")
    client._mark_price["EURUSD"] = 1.1000

    fc = FinancingCosts(admin_apr=0.025, finance_apr=0.05)
    client.set_financing_costs("GOLD", fc)
    # EURUSD has no financing config

    # Open both
    await client.place_market_order("GOLD", "BUY", size=1.0)
    await client.place_market_order("EURUSD", "BUY", size=1.0)

    # Cross a day boundary
    client._set_current_timestamp("2025-01-07T09:00:00Z")
    client._mark_price["GOLD"] = 3200.0
    client._mark_price["EURUSD"] = 1.1000

    # Only GOLD should have been charged
    expected_gold = 3200.0 * (0.025 + 0.05) / 365
    assert client.realised_pnl == pytest.approx(-expected_gold, rel=1e-6)


@pytest.mark.asyncio
async def test_financing_uses_mark_price_at_crossing() -> None:
    """Financing notional is computed from the mark price at the day boundary."""
    client = _client_with_prices(3200.0, ts="2025-01-06T09:00:00Z")

    fc = FinancingCosts(admin_apr=0.0, finance_apr=0.10)  # 10% APR for easy math
    client.set_financing_costs("GOLD", fc)

    await client.place_market_order("GOLD", "BUY", size=1.0)

    # Price rose before overnight crossing
    client._mark_price["GOLD"] = 3300.0
    client._set_current_timestamp("2025-01-07T09:00:00Z")

    # Notional should use 3300 (the mark price when crossing detected)
    expected = 3300.0 * 0.10 / 365
    assert client.realised_pnl == pytest.approx(-expected, rel=1e-6)


@pytest.mark.asyncio
async def test_weekend_hold_thu_to_mon() -> None:
    """Thu→Mon hold: Thu(1×), Fri(3×), Sat(1×), Sun(1×) = 6 financing days."""
    # 2025-01-09 is Thursday
    client = _client_with_prices(1000.0, ts="2025-01-09T09:00:00Z")

    fc = FinancingCosts(admin_apr=0.0, finance_apr=0.365)  # 0.365 APR = £1/day per £1000
    client.set_financing_costs("GOLD", fc)

    await client.place_market_order("GOLD", "BUY", size=1.0)

    # Jump straight to Monday
    client._set_current_timestamp("2025-01-13T09:00:00Z")
    client._mark_price["GOLD"] = 1000.0

    # Thu(3)→Fri(4)=1, Fri(4)→Sat(5)=3, Sat(5)→Sun(6)=1, Sun(6)→Mon(0)=1 => 6 financing days
    daily = 1000.0 * 0.365 / 365  # = 1.0
    expected = daily * 6
    assert client.realised_pnl == pytest.approx(-expected, rel=1e-6)


@pytest.mark.asyncio
async def test_position_closed_event_default_zero_financing() -> None:
    """Without financing config, PositionClosedEvent financing fields are 0."""
    from tradedesk.events import get_dispatcher
    from tradedesk.recording import PositionClosedEvent

    dispatcher = get_dispatcher()
    closed_events: list[PositionClosedEvent] = []

    async def capture(ev: PositionClosedEvent) -> None:
        closed_events.append(ev)

    dispatcher.subscribe(PositionClosedEvent, capture)

    try:
        client = _client_with_prices(100.0, ts="2025-01-06T09:00:00Z")
        await client.place_market_order("GOLD", "BUY", size=1.0)

        client._set_current_timestamp("2025-01-07T09:00:00Z")
        client._mark_price["GOLD"] = 110.0
        await client.place_market_order("GOLD", "SELL", size=1.0)

        assert len(closed_events) == 1
        assert closed_events[0].financing_cost == 0.0
        assert closed_events[0].admin_cost == 0.0
    finally:
        dispatcher.unsubscribe(PositionClosedEvent, capture)
