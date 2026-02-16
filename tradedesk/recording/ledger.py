import csv
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from pathlib import Path
from typing import Any

from tradedesk.time_utils import parse_timestamp

from .metrics import round_trips_from_fills
from .opportunity import OpportunityRecorder
from .types import EquityRecord, RecordingMode, TradeRecord


def trade_rows_from_trades(trades: list[TradeRecord]) -> list[dict[str, str]]:
    """Convert TradeRecord-like objects into the dict rows expected by
    ``round_trips_from_fills`` and related helpers.
    """
    return [
        {
            "timestamp": t.timestamp,
            "instrument": t.instrument,
            "direction": t.direction,
            "size": str(t.size),
            "price": str(t.price),
            "reason": t.reason,
        }
        for t in trades
    ]


@dataclass
class TradeLedger:
    trades: list[TradeRecord] = field(default_factory=list)
    equity: list[EquityRecord] = field(default_factory=list)
    opportunity: OpportunityRecorder = field(default_factory=OpportunityRecorder)
    mode: RecordingMode = RecordingMode.BACKTEST
    out_dir: Path | None = None  # Required for broker mode
    initial_balance: float = 10000.0  # Starting equity for broker mode
    _current_balance: float | None = None  # Running balance for broker mode
    _open_positions: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # Track positions for P&L calc
    _last_equity_date: str | None = None  # Track last daily equity write (YYYY-MM-DD)

    def __post_init__(self) -> None:
        if self.mode == RecordingMode.BROKER:
            if self.out_dir is None:
                raise ValueError("out_dir required for BROKER mode")
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self._current_balance = self.initial_balance
            # Create trades.csv with header immediately
            self._initialize_trades_csv()

    def write(self, out_dir: Path) -> None:
        """Batch write all files (backtest) or finalize (broker)"""
        out_dir.mkdir(parents=True, exist_ok=True)

        if self.mode == RecordingMode.BACKTEST:
            # Backtest: write all 6 files
            self.write_trades_csv(out_dir / "trades.csv")
            self.write_round_trips_csv(out_dir / "round_trips.csv")
            self.write_equity_csv(out_dir / "equity.csv")
            self.write_equity_daily_csv(out_dir / "equity_daily.csv")
            self.write_exposure_csv(out_dir / "exposure.csv")
            self.write_opportunity_csv(out_dir / "opportunity.csv")
        else:
            # Broker: trades.csv already written incrementally
            # Write final daily equity entry if needed (already written via _append_daily_equity)
            pass

    def record_trade(self, record: TradeRecord) -> None:
        self.trades.append(record)

        if self.mode == RecordingMode.BROKER:
            # Append to CSV immediately
            self._append_trade_to_csv(record)
            # Update synthetic equity
            self._update_synthetic_equity(record)

    def record_equity(self, record: EquityRecord) -> None:
        if self.mode == RecordingMode.BROKER:
            # Broker mode: ignore equity records (we compute synthetic equity)
            return

        # Backtest mode: record as before
        # Portfolio runs may call record_equity once per instrument per candle.
        # Coalesce by timestamp to ensure one equity row per time step.
        if self.equity and self.equity[-1].timestamp == record.timestamp:
            self.equity[-1] = record
            return
        self.equity.append(record)

    def write_trades_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                ["timestamp", "instrument", "direction", "size", "price", "reason"]
            )
            for t in self.trades:
                w.writerow(
                    [
                        t.timestamp,
                        t.instrument,
                        t.direction,
                        round(t.size, 4),
                        t.price,
                        t.reason,
                    ]
                )

    def write_round_trips_csv(self, path: Path) -> None:
        """Write reconstructed round trips.

        This is derived from trades.csv fills using the same pairing logic as metrics.

        Output schema:
        instrument,direction,entry_ts,exit_ts,entry_price,exit_price,size,pnl,hold_minutes,exit_reason
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        trade_rows = [
            {
                "instrument": t.instrument,
                "direction": t.direction,
                "timestamp": t.timestamp,
                "price": str(t.price),
                "size": str(t.size),
                "reason": t.reason,
            }
            for t in self.trades
        ]

        trips = round_trips_from_fills(trade_rows)

        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "instrument",
                    "direction",
                    "entry_ts",
                    "exit_ts",
                    "entry_price",
                    "exit_price",
                    "size",
                    "pnl",
                    "hold_minutes",
                    "exit_reason",
                ]
            )

            for t in trips:
                hold: float | str
                try:
                    hold = (
                        parse_timestamp(t.exit_ts) - parse_timestamp(t.entry_ts)
                    ).total_seconds() / 60.0
                except Exception:
                    hold = ""

                w.writerow(
                    [
                        t.instrument,
                        t.direction.value.upper(),  # Convert "long"/"short" to "LONG"/"SHORT"
                        t.entry_ts,
                        t.exit_ts,
                        t.entry_price,
                        t.exit_price,
                        round(t.size, 2),
                        round(t.pnl, 2),
                        hold,
                        t.exit_reason or "",
                    ]
                )

    def write_equity_csv(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "equity"])
            for e in self.equity:
                w.writerow([e.timestamp, round(e.equity, 2)])

    def write_equity_daily_csv(self, path: Path) -> None:
        """
        Write a daily equity snapshot series with:
        - date format: YYYY-MM-DD (no time component)
        - no gaps: emits one row per calendar day across the run span (inclusive)
        - forward-filled equity for missing days (weekends/holidays)

        Output schema:
        date,equity
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        if not self.equity:
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "equity"])
            return

        by_date: dict[str, float] = {}
        for e in self.equity:
            try:
                dt = parse_timestamp(e.timestamp).astimezone(timezone.utc)
                day = dt.date().isoformat()
                by_date[day] = float(e.equity)
            except ValueError as ex:
                raise ValueError(
                    f"Failed to parse equity timestamp: {e.timestamp!r}"
                ) from ex

        start_dt = (
            parse_timestamp(self.equity[0].timestamp).astimezone(timezone.utc).date()
        )
        end_dt = (
            parse_timestamp(self.equity[-1].timestamp).astimezone(timezone.utc).date()
        )

        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "equity"])

            cur = start_dt
            last_equity: float | None = None

            while cur <= end_dt:
                key = cur.isoformat()
                if key in by_date:
                    last_equity = by_date[key]
                if last_equity is not None:
                    w.writerow([key, round(last_equity, 2)])
                else:
                    first_key = min(by_date.keys())
                    last_equity = by_date[first_key]
                    w.writerow([key, round(last_equity, 2)])

                cur += timedelta(days=1)

    def write_exposure_csv(self, path: Path) -> None:
        """
        Write exposure over time derived from trades and equity timestamps.

        Output schema:
        timestamp,open_positions,open_instruments
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        trade_rows = [
            {
                "instrument": t.instrument,
                "direction": t.direction,
                "timestamp": t.timestamp,
                "price": str(t.price),
                "size": str(t.size),
            }
            for t in self.trades
        ]

        trips = round_trips_from_fills(trade_rows)

        windows = [
            (parse_timestamp(t.entry_ts), parse_timestamp(t.exit_ts), t.instrument)
            for t in trips
        ]

        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["timestamp", "open_positions", "open_instruments"])

            for e in self.equity:
                ts = parse_timestamp(e.timestamp)

                open_trips = [inst for start, end, inst in windows if start <= ts < end]

                w.writerow(
                    [
                        e.timestamp,
                        len(open_trips),
                        len(set(open_trips)),
                    ]
                )

    def write_opportunity_csv(self, path: Path) -> None:
        """
        Write per-instrument opportunity / activity stats derived from OpportunityRecorder.

        Output schema (example):
        instrument,active_bars,total_bars,active_ratio,avg_k_active,max_k_active
        """
        path.parent.mkdir(parents=True, exist_ok=True)

        per_instrument = getattr(self.opportunity, "per_instrument", {}) or {}

        k_active_by_ts = getattr(self.opportunity, "_k_active_by_ts", []) or []

        # _k_active_by_ts is a sequence of (timestamp, k_active)
        k_values: list[float] = []
        for item in k_active_by_ts:
            if isinstance(item, tuple) and len(item) == 2:
                k_values.append(float(item[1]))
            else:
                # Backward compatibility: allow plain numeric series
                k_values.append(float(item))

        avg_k_active = (sum(k_values) / len(k_values)) if k_values else 0.0
        max_k_active = max(k_values) if k_values else 0.0

        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "instrument",
                    "active_bars",
                    "total_bars",
                    "active_ratio",
                    "avg_k_active",
                    "max_k_active",
                ]
            )

            for instrument, stats in per_instrument.items():
                active_bars = int(getattr(stats, "active_bars", 0))
                total_bars = int(getattr(stats, "total_bars", 0))
                active_ratio = (active_bars / total_bars) if total_bars else 0.0

                w.writerow(
                    [
                        instrument,
                        active_bars,
                        total_bars,
                        float(active_ratio),
                        round(float(avg_k_active), 2),
                        max_k_active,
                    ]
                )

    def _initialize_trades_csv(self) -> None:
        """Create trades.csv with header (broker mode only)"""
        if self.out_dir is None:
            raise ValueError("out_dir is None")
        path = self.out_dir / "trades.csv"
        with path.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                ["timestamp", "instrument", "direction", "size", "price", "reason"]
            )

    def _append_trade_to_csv(self, record: TradeRecord) -> None:
        """Atomic append of single trade (broker mode only)"""
        if self.out_dir is None:
            raise ValueError("out_dir is None")
        path = self.out_dir / "trades.csv"
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    record.timestamp,
                    record.instrument,
                    record.direction,
                    record.size,
                    record.price,
                    record.reason,
                ]
            )

    def _update_synthetic_equity(self, record: TradeRecord) -> None:
        """Update running balance based on realized P&L from trades"""
        position_key = record.instrument

        if position_key not in self._open_positions:
            # Opening new position
            self._open_positions[position_key] = {
                "direction": record.direction,
                "price": record.price,
                "size": record.size,
            }
        else:
            # Check if closing or adding to position
            existing = self._open_positions[position_key]
            if existing["direction"] != record.direction:
                # Closing trade - calculate P&L
                entry_price = float(existing["price"])
                exit_price = float(record.price)
                size = min(float(existing["size"]), float(record.size))

                if existing["direction"] == "BUY":
                    pnl = (exit_price - entry_price) * size
                else:  # existing["direction"] == "SELL"
                    pnl = (entry_price - exit_price) * size

                if self._current_balance is not None:
                    self._current_balance += pnl

                # Update or remove position
                remaining_size = float(existing["size"]) - float(record.size)
                if remaining_size <= 0:
                    del self._open_positions[position_key]
                else:
                    existing["size"] = remaining_size
            else:
                # Adding to position (average price)
                total_size = float(existing["size"]) + float(record.size)
                avg_price = (
                    (float(existing["price"]) * float(existing["size"]))
                    + (float(record.price) * float(record.size))
                ) / total_size
                existing["price"] = avg_price
                existing["size"] = total_size

        # Check if day changed - write daily equity
        self._check_and_write_daily_equity(record.timestamp)

    def _check_and_write_daily_equity(self, timestamp: str) -> None:
        """Write equity_daily.csv at end of day when date changes"""
        dt = parse_timestamp(timestamp)
        current_date = dt.date().isoformat()

        if self._last_equity_date != current_date:
            # New day - append to equity_daily.csv
            if self._current_balance is not None:
                self._append_daily_equity(current_date, self._current_balance)
            self._last_equity_date = current_date

    def _append_daily_equity(self, date: str, equity: float) -> None:
        """Append daily equity snapshot (broker mode only)"""
        if self.out_dir is None:
            raise ValueError("out_dir is None")
        path = self.out_dir / "equity_daily.csv"

        # Create with header if doesn't exist
        if not path.exists():
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["date", "equity"])

        # Append daily snapshot
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            w.writerow([date, equity])
