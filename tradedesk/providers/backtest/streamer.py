import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

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

    def __init__(
        self,
        client: Any,
        candle_series: Iterable[CandleSeries],
        market_series: Iterable[MarketSeries],
    ) -> None:
        self._client = client
        self._candle_series = list(candle_series)
        self._market_series = list(market_series)
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def run(self, strategy: Any) -> None:
        await self.connect()

        stream: list[tuple[datetime, object]] = []

        # Candle events
        for cseries in self._candle_series:
            for c in cseries.candles:
                ts = _parse_ts(c.timestamp)
                self._client._set_current_timestamp(ts.isoformat())
                stream.append(
                    (
                        ts,
                        CandleClose(epic=cseries.epic, period=cseries.period, candle=c),
                    )
                )

        # Market events
        for mseries in self._market_series:
            for t in mseries.ticks:
                ts = _parse_ts(t.timestamp)
                self._client._set_current_timestamp(ts.isoformat())
                stream.append((ts, t))

        stream.sort(key=lambda x: x[0])

        try:
            for _, event in stream:
                if isinstance(event, MarketData):
                    event_ts = event.timestamp
                    # Mark-to-market uses mid price by default
                    self._client._set_mark_price(
                        event.epic, (event.bid + event.offer) / 2
                    )
                elif isinstance(event, CandleClose):
                    event_ts = event.candle.timestamp
                    self._client._set_mark_price(event.epic, event.candle.close)

                # Normalise to a stable ISO string with Z
                ts_str = event_ts.strip()
                if ts_str.endswith("Z"):
                    ts_iso = ts_str
                else:
                    # if already has +00:00 etc, keep it
                    ts_iso = ts_str.replace("+00:00", "Z")

                self._client._set_current_timestamp(ts_iso)

                await strategy._handle_event(event)
        finally:
            await self.disconnect()
