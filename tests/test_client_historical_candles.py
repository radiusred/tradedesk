import pytest

from tradedesk.providers.ig.client import IGClient
from tradedesk.chartdata import Candle


class TestClientHistoricalCandles:
    @pytest.mark.asyncio
    async def test_get_historical_candles_calls_prices_endpoint_and_parses(self, monkeypatch):
        client = IGClient()

        async def fake_request(method, path, **kwargs):
            assert method == "GET"
            # Expect 1MINUTE => MINUTE (or MINUTE_1 if you choose that mapping)
            assert path == "/prices/EPIC/MINUTE/3"
            return {
                "prices": [
                    {
                        "snapshotTimeUTC": "2025-01-01T00:00:00",
                        "openPrice": {"bid": 1.0, "ask": 1.2},
                        "highPrice": {"bid": 1.1, "ask": 1.3},
                        "lowPrice": {"bid": 0.9, "ask": 1.1},
                        "closePrice": {"bid": 1.05, "ask": 1.25},
                        "lastTradedVolume": 10,
                    }
                ]
            }

        monkeypatch.setattr(client, "_request", fake_request)

        candles = await client.get_historical_candles("EPIC", "1MINUTE", 3)

        assert isinstance(candles, list)
        assert isinstance(candles[0], Candle)
        # mid(open) = (1.0+1.2)/2 = 1.1
        assert candles[0].open == 1.1
        assert candles[0].volume == 10
