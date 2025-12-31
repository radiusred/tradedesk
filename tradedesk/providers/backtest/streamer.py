import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from tradedesk.marketdata import Candle, MarketData
from tradedesk.providers.base import Streamer
from tradedesk.marketdata import CandleClose


log = logging.getLogger(__name__)

def _parse_ts(ts: str) -> datetime:
    # Normalise common variants to something datetime.fromisoformat understands.
    # Accepts:
    # - 2025-12-04T19:20:00Z
    # - 2025/12/04T19:20:00Z
    # - 2025-12-04 19:20:00Z
    s = ts.strip()

    # Convert YYYY/MM/DD -> YYYY-MM-DD (only the date part)
    if len(s) >= 10 and s[4] == "/" and s[7] == "/":
        s = f"{s[0:4]}-{s[5:7]}-{s[8:]}"

    # Convert trailing Z to offset for fromisoformat
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"

    # Allow space separator too
    s = s.replace(" ", "T", 1)

    return datetime.fromisoformat(s)

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
                self._client._set_current_timestamp(ts.isoformat())
                stream.append((ts, CandleClose(epic=s.epic, period=s.period, candle=c)))

        # Market events
        for s in self._market_series:
            for t in s.ticks:
                ts = _parse_ts(t.timestamp)
                self._client._set_current_timestamp(ts.isoformat())
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
