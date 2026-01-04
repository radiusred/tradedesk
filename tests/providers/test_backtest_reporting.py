import pytest

from tradedesk.providers.backtest.client import BacktestClient
from tradedesk.providers.backtest.reporting import compute_equity

@pytest.mark.asyncio
async def test_compute_equity_realised_plus_unrealised():
    epic = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(epic, 100.0)
    await client.place_market_order(epic, "BUY", 2.0)
    client._set_mark_price(epic, 105.0)

    # Unrealised: (105-100)*2 = 10
    assert compute_equity(client) == 10.0

    # Close the position at 105 => realised becomes 10, unrealised 0
    await client.place_market_order(epic, "SELL", 2.0)
    assert compute_equity(client) == 10.0

@pytest.mark.asyncio
async def test_compute_equity_short_position_unrealised():
    epic = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(epic, 100.0)
    await client.place_market_order(epic, "SELL", 2.0)
    client._set_mark_price(epic, 95.0)

    # Short unrealised: (entry - mark)*size = (100-95)*2 = 10
    assert compute_equity(client) == 10.0

@pytest.mark.asyncio
async def test_compute_equity_raises_on_unknown_position_direction():
    epic = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(epic, 100.0)
    await client.place_market_order(epic, "BUY", 1.0)

    # Force an invalid direction (defensive test)
    client.positions[epic].direction = "SIDEWAYS"

    with pytest.raises(ValueError):
        compute_equity(client)
        
@pytest.mark.asyncio
async def test_compute_equity_requires_mark_price_for_open_positions():
    epic = "EPIC"
    client = BacktestClient(candle_series=[], market_series=[])
    await client.start()

    client._set_mark_price(epic, 100.0)
    await client.place_market_order(epic, "BUY", 1.0)

    # Remove mark price to simulate missing data
    client._mark_price.clear()

    with pytest.raises(RuntimeError):
        compute_equity(client)
