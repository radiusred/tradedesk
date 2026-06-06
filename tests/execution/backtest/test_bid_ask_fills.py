"""Tests for bid/ask-aware fill pricing and transaction cost overlays."""

from __future__ import annotations

import logging

import pytest

from tradedesk.execution.backtest.client import BacktestClient, TransactionCosts
from tradedesk.execution.backtest.streamer import CandleSeries
from tradedesk.types import Candle


def _make_candle(ts: str, close: float) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close)


def _client_with_bid_ask(
    bid_close: float,
    ask_close: float,
    ts: str = "2025-01-01T00:00:00Z",
) -> BacktestClient:
    """Create a BacktestClient with both bid and ask prices set."""
    candle = _make_candle(ts, bid_close)
    ask_candle = _make_candle(ts, ask_close)

    client = BacktestClient.from_history({("INST", "1MIN"): [candle]})
    client.set_ask_series(
        [CandleSeries(instrument="INST", period="1MIN", candles=[ask_candle])]
    )
    client._mark_price["INST"] = bid_close
    client._ask_price["INST"] = ask_close
    client._current_timestamp = ts
    return client


@pytest.mark.asyncio
async def test_buy_fills_at_ask() -> None:
    """BUY orders must fill at the ask price, not the bid."""
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.5)
    await client.start()

    result = await client.place_market_order("INST", "BUY", size=1.0)

    assert result["price"] == 100.5
    assert client.trades[0].price == 100.5
    assert client.trades[0].raw_price == pytest.approx(100.25)  # mid = (100 + 100.5) / 2
    assert client.trades[0].spread_cost == pytest.approx(0.25)  # half-spread


@pytest.mark.asyncio
async def test_sell_fills_at_bid() -> None:
    """SELL orders must fill at the bid price."""
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.5)
    await client.start()

    result = await client.place_market_order("INST", "SELL", size=1.0)

    assert result["price"] == 100.0
    assert client.trades[0].price == 100.0
    assert client.trades[0].raw_price == pytest.approx(100.25)
    assert client.trades[0].spread_cost == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_round_trip_pnl_includes_spread() -> None:
    """A long round trip PnL is (bid_exit - ask_entry) * size."""
    bid_entry = 100.0
    ask_entry = 100.5
    bid_exit = 102.0
    ask_exit = 102.5

    client = _client_with_bid_ask(bid_close=bid_entry, ask_close=ask_entry)
    await client.start()

    await client.place_market_order("INST", "BUY", size=2.0)  # fills at 100.5

    # Update prices for exit
    client._mark_price["INST"] = bid_exit
    client._ask_price["INST"] = ask_exit
    client._current_timestamp = "2025-01-01T01:00:00Z"

    await client.place_market_order("INST", "SELL", size=2.0)  # fills at 102.0

    # PnL = (102.0 - 100.5) * 2 = 3.0
    assert client.realised_pnl == pytest.approx(3.0)
    assert client.positions == {}


@pytest.mark.asyncio
async def test_no_ask_series_falls_back_to_bid() -> None:
    """When no ask series is set, all fills use bid price (no spread cost)."""
    candle = _make_candle("2025-01-01T00:00:00Z", 100.0)
    client = BacktestClient.from_history({("INST", "1MIN"): [candle]})
    client._mark_price["INST"] = 100.0
    client._current_timestamp = "2025-01-01T00:00:00Z"
    await client.start()

    result = await client.place_market_order("INST", "BUY", size=1.0)

    assert result["price"] == 100.0
    assert client.trades[0].spread_cost == 0.0
    assert client.trades[0].raw_price == 100.0  # falls back to bid price when no ask data


@pytest.mark.asyncio
async def test_missing_ask_price_at_fill_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    """When ask series is registered but ask price not yet set, a warning is emitted."""
    candle = _make_candle("2025-01-01T00:00:00Z", 100.0)
    ask_candle = _make_candle("2025-01-02T00:00:00Z", 100.5)  # different timestamp

    client = BacktestClient.from_history({("INST", "1MIN"): [candle]})
    client.set_ask_series(
        [CandleSeries(instrument="INST", period="1MIN", candles=[ask_candle])]
    )
    client._mark_price["INST"] = 100.0
    client._current_timestamp = "2025-01-01T00:00:00Z"
    # _ask_price["INST"] is NOT set — simulates a gap in ask data
    await client.start()

    with caplog.at_level(logging.WARNING, logger="tradedesk.execution.backtest.client"):
        result = await client.place_market_order("INST", "BUY", size=1.0)

    assert result["price"] == 100.0  # fell back to bid
    assert any("Missing ask price" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_slippage_points_applied_adversely() -> None:
    """Fixed slippage in points worsens the fill price for both BUY and SELL."""
    tc = TransactionCosts(slippage_points=0.5)
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.5)
    client.set_transaction_costs(tc)
    await client.start()

    # BUY: fills at ask + slippage = 100.5 + 0.5 = 101.0
    result_buy = await client.place_market_order("INST", "BUY", size=1.0)
    assert result_buy["price"] == pytest.approx(101.0)
    assert client.trades[0].slippage_cost == pytest.approx(0.5)

    # SELL: fills at bid - slippage = 100.0 - 0.5 = 99.5
    client._mark_price["INST"] = 100.0
    client._ask_price["INST"] = 100.5
    result_sell = await client.place_market_order("INST", "SELL", size=1.0)
    assert result_sell["price"] == pytest.approx(99.5)
    assert client.trades[1].slippage_cost == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_slippage_bps_applied() -> None:
    """Proportional slippage in bps is applied to fill price."""
    tc = TransactionCosts(slippage_bps=10)  # 10 bps = 0.1%
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.5)
    client.set_transaction_costs(tc)
    await client.start()

    # BUY: slippage = 100.5 * 10 / 10000 = 0.1005; fill = 100.5 + 0.1005 = 100.6005
    result = await client.place_market_order("INST", "BUY", size=1.0)
    assert result["price"] == pytest.approx(100.5 + 100.5 * 10 / 10_000)


@pytest.mark.asyncio
async def test_commission_per_fill_deducted_from_realised_pnl() -> None:
    """Commission per fill is deducted from realised PnL on every order."""
    tc = TransactionCosts(commission_per_fill=1.5)
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.0)
    client.set_transaction_costs(tc)
    await client.start()

    await client.place_market_order("INST", "BUY", size=1.0)
    # After entry: -1.5 commission
    assert client.realised_pnl == pytest.approx(-1.5)
    assert client.trades[0].commission_cost == pytest.approx(1.5)

    client._mark_price["INST"] = 110.0
    client._ask_price["INST"] = 110.0
    client._current_timestamp = "2025-01-01T01:00:00Z"

    await client.place_market_order("INST", "SELL", size=1.0)
    # After exit: gross PnL = 10.0, commissions = 2 * 1.5 = 3.0 → net = 7.0
    assert client.realised_pnl == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_commission_per_round_trip_deducted_at_close() -> None:
    """Commission per round trip is charged once at position close."""
    tc = TransactionCosts(commission_per_round_trip=5.0)
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.0)
    client.set_transaction_costs(tc)
    await client.start()

    await client.place_market_order("INST", "BUY", size=1.0)
    assert client.realised_pnl == pytest.approx(0.0)  # no commission at entry

    client._mark_price["INST"] = 110.0
    client._ask_price["INST"] = 110.0
    client._current_timestamp = "2025-01-01T01:00:00Z"

    await client.place_market_order("INST", "SELL", size=1.0)
    # gross = 10.0, round-trip commission = 5.0 → net = 5.0
    assert client.realised_pnl == pytest.approx(5.0)


@pytest.mark.asyncio
async def test_position_closed_event_carries_cost_fields() -> None:
    """PositionClosedEvent includes full cost decomposition from the client."""
    from tradedesk.events import get_dispatcher
    from tradedesk.recording import PositionClosedEvent

    dispatcher = get_dispatcher()
    closed_events: list[PositionClosedEvent] = []

    async def capture(ev: PositionClosedEvent) -> None:
        closed_events.append(ev)

    dispatcher.subscribe(PositionClosedEvent, capture)

    tc = TransactionCosts(slippage_points=0.1, commission_per_fill=0.5)
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.5)
    client.set_transaction_costs(tc)
    await client.start()

    await client.place_market_order("INST", "BUY", size=1.0)

    client._mark_price["INST"] = 102.0
    client._ask_price["INST"] = 102.6
    client._current_timestamp = "2025-01-01T01:00:00Z"
    await client.place_market_order("INST", "SELL", size=1.0)

    assert len(closed_events) == 1
    ev = closed_events[0]
    assert ev.raw_entry_price == pytest.approx(100.25)  # mid
    assert ev.raw_exit_price == pytest.approx(102.3)   # mid
    assert ev.entry_spread_cost == pytest.approx(0.25)
    assert ev.exit_spread_cost == pytest.approx(0.3)
    assert ev.entry_slippage_cost == pytest.approx(0.1)
    assert ev.exit_slippage_cost == pytest.approx(0.1)
    assert ev.entry_commission_cost == pytest.approx(0.5)
    assert ev.exit_commission_cost == pytest.approx(0.5)

    dispatcher.unsubscribe(PositionClosedEvent, capture)


@pytest.mark.asyncio
async def test_anomalous_ask_price_falls_back_to_bid(caplog: pytest.LogCaptureFixture) -> None:
    """Ask prices diverging >5% from bid are treated as corrupted data."""
    # bid=10250, ask=1.025 simulates corrupted Dukascopy decimal-vs-pipette data
    client = _client_with_bid_ask(bid_close=10250.0, ask_close=1.025)
    await client.start()

    with caplog.at_level(logging.WARNING, logger="tradedesk.execution.backtest.client"):
        result = await client.place_market_order("INST", "BUY", size=1.0)

    # Should fall back to bid-only pricing
    assert result["price"] == 10250.0
    assert client.trades[0].spread_cost == 0.0
    assert client.trades[0].raw_price == 10250.0
    assert any("Anomalous ask price" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_normal_spread_not_rejected() -> None:
    """Normal bid/ask spreads within 5% are used as-is."""
    # ~0.5% spread — well within threshold
    client = _client_with_bid_ask(bid_close=100.0, ask_close=100.5)
    await client.start()

    result = await client.place_market_order("INST", "BUY", size=1.0)

    assert result["price"] == 100.5  # fills at ask, not fallback


@pytest.mark.asyncio
async def test_partial_close_emits_event_and_single_round_trip_commission() -> None:
    """Open 2 / close 1 / close 1 emits a PositionClosedEvent on each exit and
    charges exactly one round-trip commission in total (RAD-3731)."""
    from tradedesk.events import get_dispatcher
    from tradedesk.recording import PositionClosedEvent, PositionOpenedEvent

    dispatcher = get_dispatcher()
    opened: list[PositionOpenedEvent] = []
    closed: list[PositionClosedEvent] = []

    async def on_open(ev: PositionOpenedEvent) -> None:
        opened.append(ev)

    async def on_close(ev: PositionClosedEvent) -> None:
        closed.append(ev)

    dispatcher.subscribe(PositionOpenedEvent, on_open)
    dispatcher.subscribe(PositionClosedEvent, on_close)
    try:
        tc = TransactionCosts(commission_per_round_trip=5.0)
        client = _client_with_bid_ask(bid_close=100.0, ask_close=100.0)
        client.set_transaction_costs(tc)
        await client.start()

        # Open a LONG of size 2 at 100.
        await client.place_market_order("INST", "BUY", size=2.0)
        assert len(opened) == 1
        assert opened[0].size == pytest.approx(2.0)
        assert client.realised_pnl == pytest.approx(0.0)  # no commission at entry

        # Mark up to 110 and scale out half (close 1 of 2): partial close.
        client._mark_price["INST"] = 110.0
        client._ask_price["INST"] = 110.0
        client._current_timestamp = "2025-01-01T01:00:00Z"
        await client.place_market_order("INST", "SELL", size=1.0)

        # Partial close MUST emit a PositionClosedEvent for the closed portion.
        assert len(closed) == 1
        first = closed[0]
        assert first.size == pytest.approx(1.0)
        # gross = (110-100)*1 = 10, half round-trip = 2.5 → net pnl = 7.5
        assert first.pnl == pytest.approx(7.5)
        # Position still open with the remaining size 1.
        assert client.positions["INST"].size == pytest.approx(1.0)

        # Close the remaining 1: full close.
        client._current_timestamp = "2025-01-01T02:00:00Z"
        await client.place_market_order("INST", "SELL", size=1.0)

        assert len(closed) == 2
        second = closed[1]
        assert second.size == pytest.approx(1.0)
        assert second.pnl == pytest.approx(7.5)  # gross 10 - half round-trip 2.5
        # Position fully closed and removed.
        assert "INST" not in client.positions

        # Exactly one round-trip commission (5.0) charged across both exits:
        # gross PnL = 20, total realised = 20 - 5 = 15.
        assert client.realised_pnl == pytest.approx(15.0)
        # Only the initial open emitted a PositionOpenedEvent (no residual flip).
        assert len(opened) == 1
    finally:
        dispatcher.unsubscribe(PositionOpenedEvent, on_open)
        dispatcher.unsubscribe(PositionClosedEvent, on_close)
