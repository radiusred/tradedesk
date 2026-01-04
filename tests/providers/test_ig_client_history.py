import pytest
from unittest.mock import AsyncMock

from tradedesk.marketdata import Candle
from tradedesk.providers.ig.client import IGClient


@pytest.mark.asyncio
async def test_period_to_rest_resolution_mapping():
    c = IGClient()
    assert c._period_to_rest_resolution("1MINUTE") == "MINUTE"
    assert c._period_to_rest_resolution("5MINUTE") == "MINUTE_5"
    assert c._period_to_rest_resolution("HOUR") == "HOUR"
    assert c._period_to_rest_resolution("HOUR_4") == "HOUR_4"
    # passthrough
    assert c._period_to_rest_resolution("MINUTE_15") == "MINUTE_15"
    # unknown passthrough
    assert c._period_to_rest_resolution("FOO") == "FOO"


@pytest.mark.asyncio
async def test_get_historical_candles_parses_mid_prices_and_sorts():
    c = IGClient()

    payload = {
        "prices": [
            # newer (out of order)
            {
                "snapshotTimeUTC": "2025-12-28T10:00:00",
                "openPrice": {"bid": 1.0, "ask": 1.2},
                "highPrice": {"bid": 1.1, "ask": 1.3},
                "lowPrice": {"bid": 0.9, "ask": 1.1},
                "closePrice": {"bid": 1.05, "ask": 1.25},
                "lastTradedVolume": 100,
            },
            # older (missing open/high/low -> should fall back to close mid)
            {
                "snapshotTime": "2025-12-28T09:00:00Z",  # already has Z
                "openPrice": {"bid": None, "ask": None},
                "highPrice": {},
                "lowPrice": None,
                "closePrice": {"bid": 2.0, "ask": 2.2},
                "lastTradedVolume": None,
            },
            # missing timestamp -> skipped
            {
                "closePrice": {"bid": 9.0, "ask": 9.2},
            },
            # missing close -> skipped
            {
                "snapshotTimeUTC": "2025-12-28T08:00:00",
                "closePrice": {"bid": None, "ask": None},
            },
        ]
    }

    c._request = AsyncMock(return_value=payload)  # type: ignore[attr-defined]

    candles = await c.get_historical_candles("EPIC", "5MINUTE", 10)

    assert len(candles) == 2
    # sorted oldest -> newest
    assert candles[0].timestamp == "2025-12-28T09:00:00Z"
    assert candles[1].timestamp == "2025-12-28T10:00:00Z"  # Z appended

    # first candle: close mid = (2.0+2.2)/2 = 2.1, others fall back to close
    assert candles[0].close == pytest.approx(2.1)
    assert candles[0].open == pytest.approx(2.1)
    assert candles[0].high == pytest.approx(2.1)
    assert candles[0].low == pytest.approx(2.1)
    assert candles[0].volume == 0.0

    # second candle: mid values
    assert candles[1].open == pytest.approx((1.0 + 1.2) / 2)
    assert candles[1].high == pytest.approx((1.1 + 1.3) / 2)
    assert candles[1].low == pytest.approx((0.9 + 1.1) / 2)
    assert candles[1].close == pytest.approx((1.05 + 1.25) / 2)
    assert candles[1].volume == 100.0


@pytest.mark.asyncio
async def test_get_historical_candles_num_points_zero_short_circuits():
    c = IGClient()
    c._request = AsyncMock()  # type: ignore[attr-defined]
    candles = await c.get_historical_candles("EPIC", "5MINUTE", 0)
    assert candles == []
    c._request.assert_not_called()
