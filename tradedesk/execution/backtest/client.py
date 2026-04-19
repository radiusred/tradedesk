import csv
import itertools
import logging
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from tradedesk.events import get_dispatcher
from tradedesk.execution.broker import AccountBalance, BrokerPosition
from tradedesk.execution.client import Client
from tradedesk.marketdata import MarketData
from tradedesk.recording import PositionClosedEvent, PositionOpenedEvent
from tradedesk.time_utils import parse_timestamp
from tradedesk.types import Candle, Direction

from .dukascopy import read_dukascopy_candles
from .streamer import (
    BacktestStreamer,
    CandleSeries,
    MarketSeries,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransactionCosts:
    """Optional transaction cost overlays applied on top of bid/ask spread.

    All fields default to zero so existing callers are unaffected.

    Attributes:
        slippage_points: Fixed adverse slippage per fill in price units.
        slippage_bps: Proportional slippage in basis points (1 bps = 0.01%).
        commission_per_fill: Fixed commission charged on every fill (£/€/$).
        commission_per_round_trip: Fixed commission charged once per closed
            round trip (£/€/$), at exit.
    """

    slippage_points: float = 0.0
    slippage_bps: float = 0.0
    commission_per_fill: float = 0.0
    commission_per_round_trip: float = 0.0


@dataclass(frozen=True)
class FinancingCosts:
    """Per-instrument overnight financing and admin fee model.

    IG charges financing + admin fees daily on open positions.  On Fridays,
    a multiplier is applied to cover the weekend (typically 3×).

    Daily charge = notional × (admin_apr + finance_apr) / 365
    where notional = size × mark_price at the overnight crossing.
    """

    admin_apr: float = 0.0
    finance_apr: float = 0.0
    friday_multiplier: int = 3


@dataclass
class Trade:
    instrument: str
    direction: Direction  # "BUY" or "SELL"
    size: float
    price: float
    timestamp: str | None = None
    raw_price: float = 0.0
    spread_cost: float = 0.0
    slippage_cost: float = 0.0
    commission_cost: float = 0.0


@dataclass
class Position:
    instrument: str
    direction: Direction
    size: float
    entry_price: float
    raw_entry_price: float = 0.0
    entry_spread_cost: float = 0.0
    entry_slippage_cost: float = 0.0
    entry_commission_cost: float = 0.0
    financing_cost_accrued: float = 0.0
    admin_cost_accrued: float = 0.0
    last_financing_date: date | None = None
    strategy: str = ""
    position_id: str = ""


class BacktestClient(Client):
    """
    Backtesting client.

    - start/close are no-ops
    - get_historical_candles serves from in-memory history
    - get_streamer replays CandleClosedEvent events
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
        self._ask_series: list[CandleSeries] = []

        self._history: dict[tuple[str, str], list[Candle]] = {
            (s.instrument, s.period): list(s.candles) for s in candle_series
        }

        self._started = False
        self._closed = False

        self._mark_price: dict[str, float] = {}
        self._ask_price: dict[str, float] = {}
        self.trades: list[Trade] = []
        self.positions: dict[str, Position] = {}
        self.realised_pnl: float = 0.0
        self._current_timestamp: str | None = None
        self._transaction_costs: TransactionCosts = TransactionCosts()
        self._financing_costs: dict[str, FinancingCosts] = {}
        self._last_event_date: date | None = None

    @classmethod
    def from_history(cls, history: dict[tuple[str, str], list[Candle]]) -> "BacktestClient":
        series: list[CandleSeries] = []
        for (instrument, period), candles in history.items():
            series.append(CandleSeries(instrument=instrument, period=period, candles=list(candles)))
        return cls(series, [])

    @classmethod
    def from_lazy_sources(
        cls,
        *,
        history: dict[tuple[str, str], list[Candle]],
        candle_series: list[CandleSeries],
        market_series: list[MarketSeries] | None = None,
    ) -> "BacktestClient":
        """Create a client with a pre-populated warmup history and lazy candle generators.

        ``history`` is served by :meth:`get_historical_candles` during strategy
        warmup.  ``candle_series`` holds the generators consumed by
        :class:`BacktestStreamer` during replay — typically the full date range.

        Unlike the default constructor, the warmup slice (``history``) and the
        streaming generators are kept separate so only a small fraction of the
        total data needs to be in memory at startup.
        """
        inst = cls.__new__(cls)
        inst._candle_series = list(candle_series)
        inst._market_series = list(market_series or [])
        inst._ask_series = []
        inst._history = dict(history)
        inst._started = False
        inst._closed = False
        inst._mark_price = {}
        inst._ask_price = {}
        inst.trades = []
        inst.positions = {}
        inst.realised_pnl = 0.0
        inst._current_timestamp = None
        inst._transaction_costs = TransactionCosts()
        inst._financing_costs = {}
        inst._last_event_date = None
        return inst

    @classmethod
    def from_dukascopy_cache(
        cls,
        cache_dir: str | Path,
        *,
        symbol: str,
        instrument: str,
        period: str,
        date_from: date,
        date_to: date,
        price_side: str = "bid",
    ) -> "BacktestClient":
        """
        Load a candle series from a Dukascopy tick cache and return a BacktestClient.

        Args:
            cache_dir: Root of the Dukascopy cache directory.
            symbol: Symbol folder name in the cache (e.g. ``"EURUSD"``).
            instrument: Instrument identifier used by the strategy.
            period: Tradedesk period string (e.g. ``"1MIN"``, ``"15MIN"``, ``"1H"``).
            date_from: First date to include (inclusive).
            date_to: Last date to include (inclusive).
            price_side: ``"bid"`` (default), ``"ask"``, or ``"mid"``.
        """
        candles = read_dukascopy_candles(
            Path(cache_dir),
            symbol,
            period,
            date_from,
            date_to,
            price_side=price_side,
        )
        return cls.from_history({(instrument, period): candles})

    @classmethod
    def from_market_csv(
        cls,
        path: str | Path,
        *,
        instrument: str,
        delimiter: str = ",",
    ) -> "BacktestClient":
        return cls.from_market_csvs({instrument: path}, delimiter=delimiter)

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

        for instrument, path in files.items():
            path = Path(path)

            ticks: list[MarketData] = []
            with path.open("r", newline="") as f:
                reader = csv.DictReader(f, delimiter=delimiter)
                if reader.fieldnames is None:
                    raise ValueError("CSV has no header row")

                header_map = {norm(h): h for h in reader.fieldnames if h is not None}

                ts_key = next((header_map[a] for a in ts_aliases if a in header_map), None)
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
                    raise ValueError(f"CSV missing required columns: {', '.join(missing)}")

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
                            instrument=instrument,
                            bid=bid,
                            offer=offer,
                            timestamp=ts_norm,
                            raw={"bid": bid, "offer": offer},
                        )
                    )

            market_series.append(MarketSeries(instrument=instrument, ticks=ticks))

        # No candle history for tick-only backtest (for now)
        return cls(candle_series=[], market_series=market_series)

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._closed = True

    def set_ask_series(self, ask_series: list[CandleSeries]) -> None:
        """Register ask-side candle series for bid/ask-aware fill pricing."""
        self._ask_series = list(ask_series)

    def set_transaction_costs(self, tc: TransactionCosts) -> None:
        """Configure transaction cost overlays (slippage and commission)."""
        self._transaction_costs = tc

    def set_financing_costs(self, instrument: str, fc: FinancingCosts) -> None:
        """Configure overnight financing/admin fee model for an instrument."""
        self._financing_costs[instrument] = fc

    def get_streamer(self) -> BacktestStreamer:
        return BacktestStreamer(
            self, self._candle_series, self._market_series, ask_series=self._ask_series
        )

    def _set_current_timestamp(self, ts: str) -> None:
        self._current_timestamp = ts
        current_date = parse_timestamp(ts).date()
        if (
            self._financing_costs
            and self.positions
            and self._last_event_date is not None
            and current_date > self._last_event_date
        ):
            self._apply_overnight_financing(self._last_event_date, current_date)
        self._last_event_date = current_date

    def _set_mark_price(self, instrument: str, price: float) -> None:
        self._mark_price[instrument] = float(price)

    def _set_ask_price(self, instrument: str, price: float) -> None:
        self._ask_price[instrument] = float(price)

    def _apply_overnight_financing(self, prev_date: date, current_date: date) -> None:
        """Debit overnight financing/admin fees for all open positions.

        Called when the event stream crosses a day boundary.  For each calendar
        day between *prev_date* (exclusive) and *current_date* (inclusive), the
        charge is ``notional × rate / 365``.  Fridays carry a configurable
        multiplier (default 3×) to cover the weekend.
        """
        for instrument, pos in list(self.positions.items()):
            fc = self._financing_costs.get(instrument)
            if fc is None or (fc.admin_apr == 0.0 and fc.finance_apr == 0.0):
                continue

            start = pos.last_financing_date if pos.last_financing_date is not None else prev_date
            if current_date <= start:
                continue

            mark = self._mark_price.get(instrument)
            if mark is None:
                continue

            notional = pos.size * mark
            financing_days = 0
            d = start
            while d < current_date:
                financing_days += fc.friday_multiplier if d.weekday() == 4 else 1
                d += timedelta(days=1)

            admin_charge = notional * fc.admin_apr / 365 * financing_days
            finance_charge = notional * fc.finance_apr / 365 * financing_days

            pos.admin_cost_accrued += admin_charge
            pos.financing_cost_accrued += finance_charge
            self.realised_pnl -= admin_charge + finance_charge
            pos.last_financing_date = current_date

    def _get_mark_price(self, instrument: str) -> float:
        if instrument not in self._mark_price:
            raise RuntimeError(f"No mark price available for {instrument} (no data replayed yet)")
        return self._mark_price[instrument]

    def get_mark_price(self, instrument: str) -> float | None:
        return self._mark_price.get(instrument)

    def compute_unrealised_pnl(self) -> float:
        """Compute unrealised PnL for all open positions using the latest mark price."""
        unreal = 0.0
        for instrument, pos in self.positions.items():
            mark = self.get_mark_price(instrument)
            if mark is None:
                raise RuntimeError(
                    f"No mark price available for {instrument} (no data replayed yet)"
                )

            if pos.direction == Direction.LONG:
                unreal += (mark - pos.entry_price) * pos.size
            elif pos.direction == Direction.SHORT:
                unreal += (pos.entry_price - mark) * pos.size
            else:
                raise ValueError(f"Unknown position direction: {pos.direction!r}")

        return float(unreal)

    def compute_equity(self) -> float:
        """Equity = realised PnL + unrealised PnL."""
        return float(self.realised_pnl + self.compute_unrealised_pnl())

    async def get_market_snapshot(self, instrument: str) -> dict[str, Any]:
        price = self._get_mark_price(instrument)
        # Backtest uses mid-price; bid/offer equal for now.
        return {"snapshot": {"bid": price, "offer": price}}

    async def get_historical_candles(
        self, instrument: str, period: str, num_points: int
    ) -> list[Candle]:
        if num_points <= 0:
            return []
        candles = self._history.get((instrument, period), [])
        return candles[-num_points:]

    def _compute_fill_price(
        self, instrument: str, direction: Direction
    ) -> tuple[float, float, float, float, float]:
        """Compute executable fill price with cost decomposition.

        Returns:
            (fill_price, raw_price, spread_cost, slippage_cost, commission_cost)

        ``fill_price`` is the executable price inclusive of all overlays.
        ``raw_price`` is the mid price (or bid if ask data unavailable).
        The three cost fields are in price units, except ``commission_cost``
        which is the absolute monetary amount for this fill.
        """
        bid_price = self._get_mark_price(instrument)
        ask_price = self._ask_price.get(instrument)

        # Guard: reject anomalous ask prices (e.g. corrupted Dukascopy data
        # where prices are in raw decimal instead of pipettes).
        if ask_price is not None and bid_price != 0:
            divergence = abs(ask_price - bid_price) / abs(bid_price)
            if divergence > 0.05:
                log.warning(
                    "Anomalous ask price for %s at %s: bid=%.6f ask=%.6f "
                    "(divergence=%.2f%%); falling back to bid-only pricing",
                    instrument,
                    self._current_timestamp,
                    bid_price,
                    ask_price,
                    divergence * 100,
                )
                ask_price = None

        # Determine executable side
        if ask_price is not None:
            # Bid/ask pricing available
            raw_price = (bid_price + ask_price) / 2
            if direction == Direction.LONG:
                exec_price = ask_price  # buy at offer
            else:
                exec_price = bid_price  # sell at bid
            spread_cost = abs(exec_price - raw_price)
        else:
            # No ask data — warn if ask series is configured (implies a gap)
            if self._ask_series:
                log.warning(
                    "Missing ask price for %s at %s; using bid price (spread cost = 0)",
                    instrument,
                    self._current_timestamp,
                )
            raw_price = bid_price
            exec_price = bid_price
            spread_cost = 0.0

        # Apply slippage overlay (adverse to the trader)
        tc = self._transaction_costs
        slippage = tc.slippage_points + exec_price * tc.slippage_bps / 10_000
        if direction == Direction.LONG:
            exec_price += slippage
        else:
            exec_price -= slippage

        return exec_price, raw_price, spread_cost, slippage, tc.commission_per_fill

    async def place_market_order(
        self,
        instrument: str,
        direction: str,
        size: float,
        currency: str = "USD",
        force_open: bool = True,
        exit_reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not self._started:
            raise RuntimeError("BacktestClient not started")

        if size <= 0:
            raise ValueError("size must be > 0")

        _direction = Direction.from_order_side(direction)
        strategy = kwargs.pop("strategy", "")
        price, raw_price, spread_cost, slippage_cost, commission_cost = self._compute_fill_price(
            instrument, _direction
        )

        self.trades.append(
            Trade(
                instrument=instrument,
                direction=_direction,
                size=float(size),
                price=price,
                timestamp=self._current_timestamp,
                raw_price=raw_price,
                spread_cost=spread_cost,
                slippage_cost=slippage_cost,
                commission_cost=commission_cost,
            )
        )

        # Deduct per-fill commission from realised PnL
        self.realised_pnl -= commission_cost

        # Very simple netting model:
        # - BUY opens/increases LONG, SELL opens/increases SHORT
        # - If opposite direction order arrives, close the entire position if sizes match.
        pos = self.positions.get(instrument)

        if pos is None:
            pid = str(uuid.uuid4())
            self.positions[instrument] = Position(
                instrument=instrument,
                direction=_direction,
                size=float(size),
                entry_price=price,
                raw_entry_price=raw_price,
                entry_spread_cost=spread_cost,
                entry_slippage_cost=slippage_cost,
                entry_commission_cost=commission_cost,
                strategy=strategy,
                position_id=pid,
            )
            # Emit PositionOpenedEvent
            await get_dispatcher().publish(
                PositionOpenedEvent(
                    instrument=instrument,
                    direction="BUY" if _direction == Direction.LONG else "SELL",
                    size=float(size),
                    entry_price=price,
                    timestamp=parse_timestamp(self._current_timestamp or ""),
                    strategy=strategy,
                    position_id=pid,
                    raw_entry_price=raw_price,
                    entry_spread_cost=spread_cost,
                    entry_slippage_cost=slippage_cost,
                    entry_commission_cost=commission_cost,
                )
            )
        else:
            if pos.direction == _direction:
                # Increase position: weighted avg entry (costs tracked from initial open only)
                new_size = pos.size + float(size)
                pos.entry_price = (pos.entry_price * pos.size + price * float(size)) / new_size
                pos.size = new_size
            else:
                # Opposite direction: close (only supports full close or reduce)
                close_size = min(pos.size, float(size))

                # Compute PnL for the closed portion
                if pos.direction == Direction.LONG:
                    closed_pnl = (price - pos.entry_price) * close_size
                else:
                    closed_pnl = (pos.entry_price - price) * close_size

                # Deduct per-round-trip commission at close
                closed_pnl -= self._transaction_costs.commission_per_round_trip

                self.realised_pnl += closed_pnl

                pos.size -= close_size
                if pos.size <= 0:
                    # Position fully closed - emit event with full cost decomposition
                    await get_dispatcher().publish(
                        PositionClosedEvent(
                            instrument=instrument,
                            direction="BUY" if pos.direction == Direction.LONG else "SELL",
                            size=close_size,
                            entry_price=pos.entry_price,
                            exit_price=price,
                            pnl=closed_pnl,
                            exit_reason=exit_reason or "market_order",
                            timestamp=parse_timestamp(self._current_timestamp or ""),
                            strategy=pos.strategy,
                            position_id=pos.position_id,
                            raw_entry_price=pos.raw_entry_price,
                            raw_exit_price=raw_price,
                            entry_spread_cost=pos.entry_spread_cost,
                            exit_spread_cost=spread_cost,
                            entry_slippage_cost=pos.entry_slippage_cost,
                            exit_slippage_cost=slippage_cost,
                            entry_commission_cost=pos.entry_commission_cost,
                            exit_commission_cost=commission_cost,
                            financing_cost=pos.financing_cost_accrued,
                            admin_cost=pos.admin_cost_accrued,
                        )
                    )
                    self.positions.pop(instrument, None)
                # If order size > position size, open residual opposite position
                residual = float(size) - close_size
                if residual > 0:
                    residual_pid = str(uuid.uuid4())
                    self.positions[instrument] = Position(
                        instrument=instrument,
                        direction=_direction,
                        size=residual,
                        entry_price=price,
                        raw_entry_price=raw_price,
                        entry_spread_cost=spread_cost,
                        entry_slippage_cost=slippage_cost,
                        entry_commission_cost=commission_cost,
                        strategy=strategy,
                        position_id=residual_pid,
                    )
                    # Emit PositionOpenedEvent for the new residual position
                    await get_dispatcher().publish(
                        PositionOpenedEvent(
                            instrument=instrument,
                            direction="BUY" if _direction == Direction.LONG else "SELL",
                            size=residual,
                            entry_price=price,
                            timestamp=parse_timestamp(self._current_timestamp or ""),
                            strategy=strategy,
                            position_id=residual_pid,
                            raw_entry_price=raw_price,
                            entry_spread_cost=spread_cost,
                            entry_slippage_cost=slippage_cost,
                            entry_commission_cost=commission_cost,
                        )
                    )

        return {
            "dealReference": f"BACKTEST-{next(self._deal_counter)}",
            "status": "FILLED",
            "instrument": instrument,
            "direction": _direction,
            "size": float(size),
            "price": price,
            "currency": currency,
        }

    async def place_market_order_confirmed(
        self,
        instrument: str,
        direction: str,
        size: float,
        currency: str = "USD",
        force_open: bool = True,
        exit_reason: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self.place_market_order(
            instrument,
            direction,
            size,
            currency=currency,
            force_open=force_open,
            exit_reason=exit_reason,
            **kwargs,
        )

    async def get_positions(self) -> list["BrokerPosition"]:
        """Return current virtual positions as BrokerPosition objects."""

        return [
            BrokerPosition(
                instrument=instrument,
                direction="BUY" if pos.direction == Direction.LONG else "SELL",
                size=pos.size,
                entry_price=pos.entry_price,
                deal_id=f"BACKTEST-{instrument}",
            )
            for instrument, pos in self.positions.items()
        ]

    async def get_account_balance(self) -> "AccountBalance":
        """Return synthetic account balance."""

        total = 10000.0 + self.realised_pnl
        return AccountBalance(
            balance=total,
            deposit=0.0,
            available=total,
            profit_loss=self.realised_pnl,
        )
