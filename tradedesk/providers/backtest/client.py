import itertools
from dataclasses import dataclass
from typing import Any
import csv
from pathlib import Path

from tradedesk.marketdata import Candle
from tradedesk.providers.base import Client
from tradedesk.marketdata import MarketData
from tradedesk.providers.backtest.streamer import (
    BacktestStreamer,
    CandleSeries,
    MarketSeries,
)


@dataclass
class Trade:
    epic: str
    direction: str  # "BUY" or "SELL"
    size: float
    price: float
    timestamp: str | None = None


@dataclass
class Position:
    epic: str
    direction: str  # "LONG" or "SHORT"
    size: float
    entry_price: float


class BacktestClient(Client):
    """
    Backtesting client.

    - start/close are no-ops
    - get_historical_candles serves from in-memory history
    - get_streamer replays CandleClose events
    - place_market_order executes virtual market fills at the latest mark price
    """

    _deal_counter = itertools.count(1)

    def __init__(
        self,
        candle_series: list[CandleSeries],
        market_series: list[MarketSeries] | None = None,
    ):
        self._candle_series = candle_series
        self._market_series = market_series or []

        self._history: dict[tuple[str, str], list[Candle]] = {
            (s.epic, s.period): list(s.candles) for s in candle_series
        }

        self._started = False
        self._closed = False

        self._mark_price: dict[str, float] = {}
        self.trades: list[Trade] = []
        self.positions: dict[str, Position] = {}
        self.realised_pnl: float = 0.0
        self._current_timestamp: str | None = None

    @classmethod
    def from_history(
        cls, history: dict[tuple[str, str], list[Candle]]
    ) -> "BacktestClient":
        series: list[CandleSeries] = []
        for (epic, period), candles in history.items():
            series.append(CandleSeries(epic=epic, period=period, candles=list(candles)))
        return cls(series, [])

    @classmethod
    def from_market_csv(
        cls,
        path: str | Path,
        *,
        epic: str,
        delimiter: str = ",",
    ) -> "BacktestClient":
        return cls.from_market_csvs({epic: path}, delimiter=delimiter)

    @classmethod
    def from_market_csvs(
        cls,
        files: dict[str, str | Path],
        *,
        delimiter: str = ",",
    ) -> "BacktestClient":
        """
        Load one or more MarketData tick streams from CSV.

        Required columns (case-insensitive):
        - timestamp (or time/datetime/date)
        - bid
        - offer
        """

        def norm(s: str) -> str:
            return s.strip().lower()

        ts_aliases = {"timestamp", "time", "datetime", "date"}

        market_series: list[MarketSeries] = []

        for epic, path in files.items():
            path = Path(path)

            ticks: list[MarketData] = []
            with path.open("r", newline="") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                if reader.fieldnames is None:
                    raise ValueError("CSV has no header row")

                header_map = {norm(h): h for h in reader.fieldnames if h is not None}

                ts_key = next(
                    (header_map[a] for a in ts_aliases if a in header_map), None
                )
                bid_key = header_map.get("bid")
                offer_key = header_map.get("offer")

                missing = [
                    name
                    for name, k in [
                        ("timestamp", ts_key),
                        ("bid", bid_key),
                        ("offer", offer_key),
                    ]
                    if k is None
                ]
                if missing:
                    raise ValueError(
                        f"CSV missing required columns: {', '.join(missing)}"
                    )

                assert ts_key and bid_key and offer_key

                for row in reader:
                    ts = (row.get(ts_key) or "").strip()
                    if not ts:
                        continue

                    if ts.endswith("Z"):
                        ts_norm = ts
                    elif "+" in ts or ts.endswith("00:00"):
                        ts_norm = ts
                    else:
                        ts_norm = ts + "Z"

                    bid = float(str(row.get(bid_key)).strip())
                    offer = float(str(row.get(offer_key)).strip())

                    ticks.append(
                        MarketData(
                            epic=epic,
                            bid=bid,
                            offer=offer,
                            timestamp=ts_norm,
                            raw={"bid": bid, "offer": offer},
                        )
                    )

            market_series.append(MarketSeries(epic=epic, ticks=ticks))

        # No candle history for tick-only backtest (for now)
        return cls(candle_series=[], market_series=market_series)

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        epic: str,
        period: str,
        timestamp_col: str | None = None,
        open_col: str | None = None,
        high_col: str | None = None,
        low_col: str | None = None,
        close_col: str | None = None,
        volume_col: str | None = None,
        tick_count_col: str | None = None,
        delimiter: str = ",",
    ) -> "BacktestClient":
        """
        Load a candle series from CSV and return a BacktestClient.

        CSV requirements:
          - timestamp column (default autodetect)
          - open/high/low/close columns (default autodetect)
          - optional volume and tick_count columns
        """
        path = Path(path)

        def norm(s: str) -> str:
            return s.strip().lower()

        # canonical -> accepted aliases
        aliases = {
            "timestamp": {"timestamp", "time", "datetime", "date"},
            "open": {"open", "o"},
            "high": {"high", "h"},
            "low": {"low", "l"},
            "close": {"close", "c"},
            "volume": {"volume", "vol", "v"},
            "tick_count": {"tick_count", "ticks", "tickcount"},
        }

        candles: list[Candle] = []

        with path.open("r", newline="") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            if reader.fieldnames is None:
                raise ValueError("CSV has no header row")

            # Build normalized header map
            header_map = {norm(h): h for h in reader.fieldnames if h is not None}

            def pick(explicit: str | None, key: str) -> str | None:
                if explicit:
                    if norm(explicit) not in header_map:
                        raise ValueError(f"CSV missing column: {explicit}")
                    return header_map[norm(explicit)]
                for a in aliases[key]:
                    if a in header_map:
                        return header_map[a]
                return None

            ts_key = pick(timestamp_col, "timestamp")
            o_key = pick(open_col, "open")
            h_key = pick(high_col, "high")
            l_key = pick(low_col, "low")
            c_key = pick(close_col, "close")
            v_key = pick(volume_col, "volume")
            t_key = pick(tick_count_col, "tick_count")

            missing = [
                name
                for name, k in [
                    ("timestamp", ts_key),
                    ("open", o_key),
                    ("high", h_key),
                    ("low", l_key),
                    ("close", c_key),
                ]
                if k is None
            ]
            if missing:
                raise ValueError(f"CSV missing required columns: {', '.join(missing)}")

            assert ts_key and o_key and h_key and l_key and c_key

            for row in reader:
                ts = (row.get(ts_key) or "").strip()
                if not ts:
                    continue

                # Normalize to ...Z when not provided
                if ts.endswith("Z"):
                    ts_norm = ts
                elif "+" in ts or ts.endswith("00:00"):
                    ts_norm = ts
                else:
                    ts_norm = ts + "Z"

                def fnum(val: str | None, default: float = 0.0) -> float:
                    if val is None:
                        return default
                    s = str(val).strip()
                    return default if s == "" else float(s)

                def inum(val: str | None, default: int = 0) -> int:
                    if val is None:
                        return default
                    s = str(val).strip()
                    return default if s == "" else int(float(s))

                candle = Candle(
                    timestamp=ts_norm,
                    open=fnum(row.get(o_key)),
                    high=fnum(row.get(h_key)),
                    low=fnum(row.get(l_key)),
                    close=fnum(row.get(c_key)),
                    volume=fnum(row.get(v_key)) if v_key else 0.0,
                    tick_count=inum(row.get(t_key)) if t_key else 0,
                )
                candles.append(candle)

        history = {(epic, period): candles}
        return cls.from_history(history)

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._closed = True

    def get_streamer(self) -> Any:
        return BacktestStreamer(self, self._candle_series, self._market_series)

    def _set_current_timestamp(self, ts: str) -> None:
        self._current_timestamp = ts

    def _set_mark_price(self, epic: str, price: float) -> None:
        self._mark_price[epic] = float(price)

    def _get_mark_price(self, epic: str) -> float:
        if epic not in self._mark_price:
            raise RuntimeError(
                f"No mark price available for {epic} (no data replayed yet)"
            )
        return self._mark_price[epic]

    def get_mark_price(self, epic: str) -> float | None:
        return self._mark_price.get(epic)

    async def get_market_snapshot(self, epic: str) -> dict[str, Any]:
        price = self._get_mark_price(epic)
        # Backtest uses mid-price; bid/offer equal for now.
        return {"snapshot": {"bid": price, "offer": price}}

    async def get_historical_candles(
        self, epic: str, period: str, num_points: int
    ) -> list[Candle]:
        if num_points <= 0:
            return []
        candles = self._history.get((epic, period), [])
        return candles[-num_points:]

    async def place_market_order(
        self,
        epic: str,
        direction: str,
        size: float,
        currency: str = "USD",
        force_open: bool = True,
    ) -> dict[str, Any]:
        if not self._started:
            raise RuntimeError("BacktestClient not started")

        if size <= 0:
            raise ValueError("size must be > 0")

        direction = direction.upper()
        if direction not in {"BUY", "SELL"}:
            raise ValueError("direction must be BUY or SELL")

        price = self._get_mark_price(epic)
        self.trades.append(
            Trade(
                epic=epic,
                direction=direction,
                size=float(size),
                price=price,
                timestamp=self._current_timestamp,
            )
        )

        # Very simple netting model:
        # - BUY opens/increases LONG, SELL opens/increases SHORT
        # - If opposite direction order arrives, close the entire position if sizes match.
        pos = self.positions.get(epic)

        if pos is None:
            self.positions[epic] = Position(
                epic=epic,
                direction="LONG" if direction == "BUY" else "SHORT",
                size=float(size),
                entry_price=price,
            )
        else:
            same = (pos.direction == "LONG" and direction == "BUY") or (
                pos.direction == "SHORT" and direction == "SELL"
            )
            if same:
                # Increase position: weighted avg entry
                new_size = pos.size + float(size)
                pos.entry_price = (
                    pos.entry_price * pos.size + price * float(size)
                ) / new_size
                pos.size = new_size
            else:
                # Opposite direction: close (only supports full close or reduce; compute realised on reduced amount)
                close_size = min(pos.size, float(size))
                if pos.direction == "LONG":
                    self.realised_pnl += (price - pos.entry_price) * close_size
                else:
                    self.realised_pnl += (pos.entry_price - price) * close_size

                pos.size -= close_size
                if pos.size <= 0:
                    self.positions.pop(epic, None)
                # If order size > position size, open residual opposite position
                residual = float(size) - close_size
                if residual > 0:
                    self.positions[epic] = Position(
                        epic=epic,
                        direction="LONG" if direction == "BUY" else "SHORT",
                        size=residual,
                        entry_price=price,
                    )

        return {
            "dealReference": f"BACKTEST-{next(self._deal_counter)}",
            "status": "FILLED",
            "epic": epic,
            "direction": direction,
            "size": float(size),
            "price": price,
            "currency": currency,
        }
