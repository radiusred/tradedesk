import asyncio
import pytest

import tradedesk.strategy as strategy_module
from tradedesk.subscriptions import MarketSubscription, ChartSubscription
import tradedesk.providers.ig.streamer as ig_streamer

class FakeUpdate:
    def __init__(self, item_name: str, values: dict[str, str | None]):
        self._item_name = item_name
        self._values = values

    def getValue(self, k: str):
        return self._values.get(k)

    def getItemName(self):
        return self._item_name


class FakeSubscription:
    def __init__(self, mode: str, items: list[str], fields: list[str]):
        self.mode = mode
        self.items = items
        self.fields = fields
        self._listeners = []

    def addListener(self, listener):
        self._listeners.append(listener)

    @property
    def listeners(self):
        return list(self._listeners)


class FakeConnectionDetails:
    def setUser(self, *_args, **_kwargs): ...
    def setPassword(self, *_args, **_kwargs): ...


class FakeLSClient:
    def __init__(self, url: str, adapter: str):
        self.url = url
        self.adapter = adapter
        self.connectionDetails = FakeConnectionDetails()
        self._listeners = []
        self.subscribed: list[FakeSubscription] = []
        self.connected = False
        self.disconnected = False

    def addListener(self, listener):
        self._listeners.append(listener)

    def connect(self):
        self.connected = True

    def subscribe(self, sub):
        self.subscribed.append(sub)
        for l in getattr(sub, "listeners", []):
            if hasattr(l, "onSubscription"):
                l.onSubscription()

    def disconnect(self):
        self.disconnected = True


@pytest.mark.asyncio
class TestRunStreaming:
    async def test_run_streaming_processes_market_and_chart_updates(self, monkeypatch, DummyStrategy, candle_factory):
        # Capture created LS client instance
        created = {"client": None}

        def ls_factory(url: str, adapter: str):
            c = FakeLSClient(url, adapter)
            created["client"] = c
            return c
        
        monkeypatch.setattr(ig_streamer, "LightstreamerClient", ls_factory)
        monkeypatch.setattr(ig_streamer, "Subscription", FakeSubscription)

        market_sub = MarketSubscription("EPIC.MKT")
        chart_sub = ChartSubscription("EPIC.CHT", "1MINUTE")

        Strat = DummyStrategy([market_sub, chart_sub])

        ClientStub = type("Client", (), {
            "ls_url": "https://example",
            "ls_cst": "CST",
            "ls_xst": "XST",
            "client_id": "CID",
            "account_id": "AID",
            "get_streamer": lambda self: ig_streamer.Lightstreamer(self),
        })
        strat = Strat(client=ClientStub())

        seen_market = []
        seen_candles = []

        async def on_price_update(md):
            seen_market.append((md))

        async def on_candle_close(cc):
            await strategy_module.BaseStrategy.on_candle_close(strat, cc)
            seen_candles.append((cc.epic, cc.period, cc.candle))

        strat.on_price_update = on_price_update  # type: ignore
        strat.on_candle_close = on_candle_close  # type: ignore

        task = asyncio.create_task(strat._run_streaming())

        try:
            # Wait for LS client instance and subscriptions
            for _ in range(100):
                await asyncio.sleep(0)
                if created["client"] is not None and created["client"].subscribed:
                    break

            ls_client: FakeLSClient = created["client"]
            assert ls_client is not None
            assert ls_client.connected is True

            # We expect 1 market subscription + 1 chart subscription
            assert len(ls_client.subscribed) == 2

            # Identify which is market vs chart by fields
            market_ls_sub = next(s for s in ls_client.subscribed if "BID" in s.fields and "OFFER" in s.fields)
            chart_ls_sub = next(s for s in ls_client.subscribed if "CONS_END" in s.fields)

            # Trigger a market update
            mu = FakeUpdate(
                item_name=market_ls_sub.items[0],
                values={"BID": "1.25", "OFFER": "1.26", "UPDATE_TIME": "12:00:00", "MARKET_STATE": "TRADEABLE"},
            )
            market_ls_sub.listeners[0].onItemUpdate(mu)

            # Trigger a completed candle update (CONS_END=1)
            cu = FakeUpdate(
                item_name=chart_ls_sub.items[0],
                values={
                    "CONS_END": "1",
                    "UTM": "2025-01-01T00:00:00Z",
                    "OFR_OPEN": "1.30",
                    "OFR_HIGH": "1.40",
                    "OFR_LOW": "1.20",
                    "OFR_CLOSE": "1.35",
                    "BID_OPEN": "1.29",
                    "BID_HIGH": "1.39",
                    "BID_LOW": "1.19",
                    "BID_CLOSE": "1.34",
                    "LTV": "1000",
                    "CONS_TICK_COUNT": "10",
                },
            )
            chart_ls_sub.listeners[0].onItemUpdate(cu)

            # Yield control to let queue consumers run
            for _ in range(20):
                await asyncio.sleep(0)
                if seen_market and seen_candles:
                    break

            assert len(seen_market) == 1
            assert seen_market[0].epic == "EPIC.MKT"

            assert len(seen_candles) == 1
            assert seen_candles[0][0] == "EPIC.CHT"
            assert seen_candles[0][1] == "1MINUTE"

            # Default on_candle_update should have stored candle in chart history
            assert len(strat.charts[("EPIC.CHT", "1MINUTE")]) == 1

        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Ensure disconnect called during cancellation cleanup
            ls_client = created["client"]
            assert ls_client is not None
            assert ls_client.disconnected is True
