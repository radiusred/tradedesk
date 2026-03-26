import heapq
import itertools
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from tradedesk.execution.streamer import Streamer
from tradedesk.marketdata import CandleClosedEvent, MarketData
from tradedesk.types import Candle

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _AskPriceUpdate:
    """Internal sentinel: updates ask price on the client without emitting an event."""

    instrument: str
    close: float


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
    instrument: str
    period: str
    candles: Iterable[Candle]


@dataclass(frozen=True)
class MarketSeries:
    instrument: str
    ticks: list[MarketData]


def _candle_gen(
    cseries: CandleSeries, seq: Iterator[int]
) -> Iterator[tuple[datetime, int, CandleClosedEvent]]:
    """Yield (timestamp, seq, CandleClosedEvent) for each candle in *cseries*."""
    for c in cseries.candles:
        ts = _parse_ts(c.timestamp)
        yield (
            ts,
            next(seq),
            CandleClosedEvent(
                instrument=cseries.instrument,
                timeframe=cseries.period,
                candle=c,
                timestamp=ts,
            ),
        )


def _ask_gen(
    cseries: CandleSeries, seq: Iterator[int]
) -> Iterator[tuple[datetime, int, _AskPriceUpdate]]:
    """Yield (timestamp, seq, _AskPriceUpdate) for each ask candle.

    Ask price updates are injected into the priority queue *before* the
    corresponding bid candle events (lower seq) so that ask prices are always
    current when a strategy fires after a bid CandleClosedEvent.
    """
    for c in cseries.candles:
        ts = _parse_ts(c.timestamp)
        yield (ts, next(seq), _AskPriceUpdate(instrument=cseries.instrument, close=c.close))


def _market_gen(
    mseries: MarketSeries, seq: Iterator[int]
) -> Iterator[tuple[datetime, int, MarketData]]:
    """Yield (timestamp, seq, MarketData) for each tick in *mseries*."""
    for t in mseries.ticks:
        ts = _parse_ts(t.timestamp)
        yield (ts, next(seq), t)


class BacktestStreamer(Streamer):
    """
    Replay streamer.

    Replays MarketData and CandleClosedEvent events in timestamp order across all
    series, calling `strategy._handle_event(...)`.

    Ask-side candle series (``ask_series``) are interleaved in the priority queue
    *before* the corresponding bid candle events so that ask prices are always
    current when a strategy fires.  Ask candles never emit ``CandleClosedEvent``
    to the consumer — they only update the client's internal ask price.
    """

    def __init__(
        self,
        client: Any,
        candle_series: Iterable[CandleSeries],
        market_series: Iterable[MarketSeries],
        ask_series: Iterable[CandleSeries] | None = None,
    ) -> None:
        self._client = client
        self._candle_series = list(candle_series)
        self._market_series = list(market_series)
        self._ask_series = list(ask_series) if ask_series is not None else []
        self._connected = False

    async def connect(self) -> None:
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False

    async def run(self, consumer: Any) -> None:
        await self.connect()

        # Shared monotonic counter used as a tiebreaker when two events share the
        # same timestamp.  It is consumed lazily as heapq.merge pulls items from
        # each per-instrument generator, guaranteeing uniqueness without requiring
        # CandleClosedEvent or MarketData to be orderable.
        seq = itertools.count()

        # Ask streams are added FIRST so their events get lower seq values and
        # are processed before bid candle events at the same timestamp.
        streams: list[Iterator[Any]] = [_ask_gen(s, seq) for s in self._ask_series]
        streams += [_candle_gen(s, seq) for s in self._candle_series]
        streams += [_market_gen(s, seq) for s in self._market_series]

        event_ts: str = ""

        try:
            for _, __, event in heapq.merge(*streams):
                if isinstance(event, _AskPriceUpdate):
                    # Update ask price only — never forward to consumer.
                    self._client._set_ask_price(event.instrument, event.close)
                    continue

                if isinstance(event, MarketData):
                    event_ts = event.timestamp
                    # Mark-to-market uses mid price by default
                    self._client._set_mark_price(event.instrument, (event.bid + event.offer) / 2)
                elif isinstance(event, CandleClosedEvent):
                    event_ts = event.candle.timestamp
                    self._client._set_mark_price(event.instrument, event.candle.close)

                # Normalise to a stable ISO string with Z
                ts_str = event_ts.strip()
                ts_iso = ts_str if ts_str.endswith("Z") else ts_str.replace("+00:00", "Z")
                self._client._set_current_timestamp(ts_iso)

                await consumer._handle_event(event)
        finally:
            await self.disconnect()
