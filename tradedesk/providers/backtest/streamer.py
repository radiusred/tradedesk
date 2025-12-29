from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from tradedesk.chartdata import Candle
from tradedesk.providers.base import Streamer
from tradedesk.providers.events import CandleClose, MarketData

log = logging.getLogger(__name__)


def _parse_ts(ts: str) -> datetime:
    # Candle timestamps should be ISO-8601; allow a trailing 'Z'
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


@dataclass(frozen=True)
class CandleSeries:
    epic: str
    period: str
    candles: list[Candle]

@dataclass(frozen=True)
class MarketSeries:
    epic: str
    ticks: list[MarketData]


class BacktestStreamer(Streamer):
    """
    Replay streamer.

    Replays MarketData and CandleClose events in timestamp order across all
    series, calling `strategy._handle_event(...)`.
    """

    def __init__(self, client, candle_series: Iterable[CandleSeries], market_series: Iterable[MarketSeries]):
        self._client = client
        self._candle_series = list(candle_series)
        self._market_series = list(market_series)
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def run(self, strategy) -> None:
        await self.connect()

        stream: list[tuple[datetime, object]] = []

        # Candle events
        for s in self._candle_series:
            for c in s.candles:
                ts = _parse_ts(c.timestamp)
                stream.append((ts, CandleClose(epic=s.epic, period=s.period, candle=c)))

        # Market events
        for s in self._market_series:
            for t in s.ticks:
                ts = _parse_ts(t.timestamp)
                stream.append((ts, t))

        stream.sort(key=lambda x: x[0])

        try:
            for _, event in stream:
                if isinstance(event, MarketData):
                    # Mark-to-market uses mid price by default
                    self._client._set_mark_price(event.epic, (event.bid + event.offer) / 2)
                elif isinstance(event, CandleClose):
                    self._client._set_mark_price(event.epic, event.candle.close)

                await strategy._handle_event(event)
        finally:
            await self.disconnect()
