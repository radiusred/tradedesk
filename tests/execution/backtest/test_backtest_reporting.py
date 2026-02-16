import pytest

from tradedesk.execution.backtest.client import BacktestClient
from tradedesk.execution.backtest.reporting import compute_equity


@pytest.mark.asyncio
async def test_compute_equity_realised_plus_unrealised():
    instrument = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(instrument, 100.0)
    await client.place_market_order(instrument, "BUY", 2.0)
    client._set_mark_price(instrument, 105.0)

    # Unrealised: (105-100)*2 = 10
    assert compute_equity(client) == 10.0

    # Close the position at 105 => realised becomes 10, unrealised 0
    await client.place_market_order(instrument, "SELL", 2.0)
    assert compute_equity(client) == 10.0


@pytest.mark.asyncio
async def test_compute_equity_short_position_unrealised():
    instrument = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(instrument, 100.0)
    await client.place_market_order(instrument, "SELL", 2.0)
    client._set_mark_price(instrument, 95.0)

    # Short unrealised: (entry - mark)*size = (100-95)*2 = 10
    assert compute_equity(client) == 10.0


@pytest.mark.asyncio
async def test_compute_equity_raises_on_unknown_position_direction():
    instrument = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(instrument, 100.0)
    await client.place_market_order(instrument, "BUY", 1.0)

    # Force an invalid direction (defensive test) - bypass type system
    object.__setattr__(client.positions[instrument], "direction", "SIDEWAYS")

    with pytest.raises(ValueError, match="Unknown position direction"):
        compute_equity(client)


@pytest.mark.asyncio
async def test_compute_equity_requires_mark_price_for_open_positions():
    instrument = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(instrument, 100.0)
    await client.place_market_order(instrument, "BUY", 1.0)

    # Remove mark price to simulate missing data
    client._mark_price.clear()

    with pytest.raises(RuntimeError):
        compute_equity(client)
