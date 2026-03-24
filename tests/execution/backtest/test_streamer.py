"""Tests for BacktestStreamer k-way merge and lazy candle iteration."""

from __future__ import annotations

import pytest

from tradedesk.execution.backtest.client import BacktestClient
from tradedesk.execution.backtest.streamer import BacktestStreamer, CandleSeries
from tradedesk.marketdata import CandleClosedEvent
from tradedesk.types import Candle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candle(ts: str, close: float = 1.0) -> Candle:
    return Candle(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


def _make_client() -> BacktestClient:
    return BacktestClient.from_history({})


# ---------------------------------------------------------------------------
# CandleSeries accepts Iterable (generator)
# ---------------------------------------------------------------------------


def test_candle_series_accepts_generator() -> None:
    """CandleSeries.candles may be a generator, not just a list."""

    def _gen():
        yield _candle("2025-01-01T00:00:00Z")
        yield _candle("2025-01-01T00:01:00Z")

    series = CandleSeries(instrument="TEST", period="1MIN", candles=_gen())
    events_seen = list(series.candles)
    assert len(events_seen) == 2


# ---------------------------------------------------------------------------
# BacktestStreamer: single series ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamer_single_series_in_order() -> None:
    """A single instrument series is dispatched in chronological order."""
    candles = [
        _candle("2025-01-01T00:00:00Z", 1.0),
        _candle("2025-01-01T00:01:00Z", 2.0),
        _candle("2025-01-01T00:02:00Z", 3.0),
    ]
    series = CandleSeries(instrument="INST", period="1MIN", candles=candles)
    client = _make_client()
    streamer = BacktestStreamer(client, [series], [])

    received: list[CandleClosedEvent] = []

    class _Consumer:
        async def _handle_event(self, event):
            received.append(event)

    await streamer.run(_Consumer())

    assert len(received) == 3
    closes = [e.candle.close for e in received]
    assert closes == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# BacktestStreamer: k-way merge across instruments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamer_two_instruments_interleaved() -> None:
    """Events from two instruments are merged in timestamp order."""
    s1 = CandleSeries(
        instrument="A",
        period="1MIN",
        candles=[
            _candle("2025-01-01T00:00:00Z", 10.0),
            _candle("2025-01-01T00:02:00Z", 30.0),
        ],
    )
    s2 = CandleSeries(
        instrument="B",
        period="1MIN",
        candles=[
            _candle("2025-01-01T00:01:00Z", 20.0),
            _candle("2025-01-01T00:03:00Z", 40.0),
        ],
    )
    client = _make_client()
    streamer = BacktestStreamer(client, [s1, s2], [])

    received: list[CandleClosedEvent] = []

    class _Consumer:
        async def _handle_event(self, event):
            received.append(event)

    await streamer.run(_Consumer())

    assert len(received) == 4
    closes = [e.candle.close for e in received]
    assert closes == [10.0, 20.0, 30.0, 40.0]


@pytest.mark.asyncio
async def test_streamer_five_instruments_sorted() -> None:
    """k-way merge produces a globally sorted stream for k=5 instruments."""
    import random

    rng = random.Random(42)

    # Assign each instrument a set of minute-offsets, then interleave manually
    all_events: list[tuple[int, str, float]] = []
    series_list = []
    for i in range(5):
        offsets = sorted(rng.sample(range(100), 10))
        candles = [_candle(f"2025-01-01T{o // 60:02d}:{o % 60:02d}:00Z", float(o)) for o in offsets]
        series_list.append(CandleSeries(instrument=f"INST{i}", period="1MIN", candles=candles))
        for o in offsets:
            all_events.append((o, f"INST{i}", float(o)))

    expected_closes = [c for _, __, c in sorted(all_events)]

    client = _make_client()
    streamer = BacktestStreamer(client, series_list, [])

    received: list[CandleClosedEvent] = []

    class _Consumer:
        async def _handle_event(self, event):
            received.append(event)

    await streamer.run(_Consumer())

    assert len(received) == 50
    actual_closes = [e.candle.close for e in received]
    assert actual_closes == expected_closes


# ---------------------------------------------------------------------------
# BacktestStreamer: lazy generator consumed once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streamer_consumes_generator_lazily() -> None:
    """A generator-backed CandleSeries is consumed without pre-materialisation."""
    consumed: list[str] = []

    def _gen():
        for ts in ["2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z"]:
            consumed.append(ts)
            yield _candle(ts)

    series = CandleSeries(instrument="TEST", period="1MIN", candles=_gen())
    client = _make_client()
    streamer = BacktestStreamer(client, [series], [])

    # Before run, nothing consumed
    assert consumed == []

    received: list[CandleClosedEvent] = []

    class _Consumer:
        async def _handle_event(self, event):
            received.append(event)

    await streamer.run(_Consumer())

    assert len(received) == 2
    # All consumed during run (lazy)
    assert len(consumed) == 2


# ---------------------------------------------------------------------------
# BacktestClient.from_lazy_sources
# ---------------------------------------------------------------------------


def test_from_lazy_sources_history_served() -> None:
    """get_historical_candles returns candles from the warmup history."""
    warmup = [_candle("2025-01-01T00:00:00Z", 1.5)]
    history = {("INST", "1MIN"): warmup}

    def _gen():
        yield _candle("2025-01-01T00:00:00Z", 1.5)
        yield _candle("2025-01-01T00:01:00Z", 2.5)

    series = CandleSeries(instrument="INST", period="1MIN", candles=_gen())
    client = BacktestClient.from_lazy_sources(history=history, candle_series=[series])

    import asyncio

    fetched = asyncio.run(client.get_historical_candles("INST", "1MIN", 10))
    assert len(fetched) == 1
    assert abs(fetched[0].close - 1.5) < 1e-9


def test_from_lazy_sources_streamer_uses_series() -> None:
    """get_streamer() returns a streamer backed by the lazy candle_series."""
    history: dict = {}
    candles = [_candle("2025-01-01T00:00:00Z")]
    series = CandleSeries(instrument="INST", period="1MIN", candles=candles)
    client = BacktestClient.from_lazy_sources(history=history, candle_series=[series])

    streamer = client.get_streamer()
    assert isinstance(streamer, BacktestStreamer)
    assert len(streamer._candle_series) == 1
    assert streamer._candle_series[0].instrument == "INST"
