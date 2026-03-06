"""Performance metrics and trade analysis."""

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from ..types import Direction


@dataclass(frozen=True)
class RoundTrip:
    """A completed round-trip trade (entry + exit)."""

    instrument: str
    direction: Direction
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    size: float
    pnl: float
    exit_reason: str | None = None


@dataclass(frozen=True)
class Metrics:
    """Performance metrics for a trading strategy."""

    trades: int
    round_trips: int
    wins: int
    losses: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    expectancy: float
    max_drawdown: float
    final_equity: float
    avg_hold_minutes: float
    exits_by_reason: dict[str, int]
    sharpe_ratio: float 


def _parse_ts(ts: str) -> datetime:
    """Parse timestamp string into datetime, handling various formats."""
    s = ts.strip()

    # Normalise YYYY/MM/DD -> YYYY-MM-DD
    if len(s) >= 10 and s[4] == "/" and s[7] == "/":
        s = f"{s[0:4]}-{s[5:7]}-{s[8:]}"

    s = s.replace("Z", "+00:00")

    # Allow space separator too
    s = s.replace(" ", "T", 1)

    return datetime.fromisoformat(s)


def max_drawdown(equity: list[float]) -> float:
    """Calculate maximum drawdown from equity curve."""
    peak = float("-inf")
    mdd = 0.0
    for x in equity:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)  # negative number
    return float(mdd)


def equity_rows_from_round_trips(
    trips: Iterable[RoundTrip], *, starting_equity: float = 0.0
) -> list[dict[str, Any]]:
    """Build a minimal equity curve from round trips by cumulatively summing PnL.

    This is primarily intended for per-instrument reporting where the ledger contains
    only portfolio-level equity snapshots.

    The returned rows match compute_metrics()' expected equity_rows schema:
      - {'timestamp': <exit_ts>, 'equity': <float as str>}
    """
    eq = float(starting_equity)
    out: list[dict[str, Any]] = []
    for t in trips:
        eq += float(t.pnl)
        out.append({"timestamp": str(t.exit_ts), "equity": str(eq)})
    return out


def round_trips_from_fills(rows: list[dict[str, Any]]) -> list[RoundTrip]:
    """
    Reconstruct round trips per instrument under the assumption:
      - one open position per instrument
      - fills alternate entry/exit

    Expected row schema:
      - 'epic' or 'instrument': instrument identifier
      - 'direction': "BUY" or "SELL"
      - 'timestamp': timestamp string
      - 'price': fill price
      - 'size': position size
      - 'reason' (optional): exit reason
    """
    open_pos: dict[str, dict[str, Any]] = {}
    trips: list[RoundTrip] = []

    for r in rows:
        # Support both 'epic' (IG terminology) and 'instrument' (generic)
        instrument = r.get("instrument") or r.get("epic", "")
        side = r["direction"]  # "BUY" or "SELL"
        ts = r["timestamp"]
        price = float(r["price"])
        size = float(r["size"])

        if instrument not in open_pos:
            # entry
            direction = Direction.LONG if side == "BUY" else Direction.SHORT
            open_pos[instrument] = {
                "direction": direction,
                "entry_ts": ts,
                "entry_price": price,
                "size": size,
            }
            continue

        # exit
        entry = open_pos.pop(instrument)
        direction = entry["direction"]
        entry_price = float(entry["entry_price"])
        entry_size = float(entry["size"])

        # If sizes ever differ, this simplistic pairing is insufficient.
        if abs(entry_size - size) > 1e-9:
            raise ValueError(
                f"Size mismatch for {instrument}: entry {entry_size} exit {size}"
            )

        pnl = (
            (price - entry_price) * size
            if direction == Direction.LONG
            else (entry_price - price) * size
        )

        exit_reason = r.get("reason") or "unknown"

        trips.append(
            RoundTrip(
                instrument=instrument,
                direction=direction,
                entry_ts=str(entry["entry_ts"]),
                exit_ts=str(ts),
                entry_price=entry_price,
                exit_price=price,
                size=size,
                pnl=float(pnl),
                exit_reason=exit_reason,
            )
        )

    return trips


def compute_metrics(
    *,
    equity_rows: list[dict[str, Any]],
    trade_rows: list[dict[str, Any]],
    reporting_scale: float = 1.0,
) -> Metrics:
    """
    Compute comprehensive performance metrics.

    Args:
        equity_rows: List of dicts with 'timestamp' and 'equity' fields
        trade_rows: List of dicts with trade fill data (see round_trips_from_fills)
        reporting_scale: Scale factor for linear metrics (default 1.0)

    Returns:
        Metrics dataclass with all performance statistics
    """
    if reporting_scale <= 0:
        raise ValueError("reporting_scale must be > 0")

    equity = [
        float(r["equity"]) for r in equity_rows if r.get("equity") not in (None, "")
    ]
    final_equity = equity[-1] if equity else 0.0

    trips = round_trips_from_fills(trade_rows)

    pnls = [t.pnl for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    rt_n = len(trips)
    wins_n = len(wins)
    losses_n = len(losses)

    avg_win = (sum(wins) / wins_n) if wins_n else 0.0
    avg_loss = (sum(losses) / losses_n) if losses_n else 0.0  # negative
    profit_factor = (
        (sum(wins) / abs(sum(losses)))
        if losses_n and abs(sum(losses)) > 0
        else float("inf")
        if wins_n
        else 0.0
    )
    win_rate = (wins_n / rt_n) if rt_n else 0.0
    expectancy = (win_rate * avg_win + (1.0 - win_rate) * avg_loss) if rt_n else 0.0

    # 2. Sharpe Ratio Calculation (Daily Basis)
    daily_pnls: dict[str, float] = {}
    for t in trips:
        # Extract date from exit timestamp (YYYY-MM-DD)
        day_key = t.exit_ts[:10] 
        daily_pnls[day_key] = daily_pnls.get(day_key, 0.0) + t.pnl
    
    daily_values = list(daily_pnls.values())
    n_days = len(daily_values)
    
    sharpe_ann = 0.0
    if n_days > 1:
        mean_d = sum(daily_values) / n_days
        # Sample standard deviation
        variance_d = sum((x - mean_d) ** 2 for x in daily_values) / (n_days - 1)
        std_d = math.sqrt(variance_d)
        
        if std_d > 0:
            # Annualize by multiplying by sqrt of 252 trading days
            sharpe_ann = (mean_d / std_d) * math.sqrt(252)

    # 3. Holding Time and Exits
    exits_by_reason: dict[str, int] = {}
    for t in trips:
        k = t.exit_reason or "unknown"
        exits_by_reason[k] = exits_by_reason.get(k, 0) + 1
    
    hold_mins: list[float] = []
    for t in trips:
        dt = _parse_ts(t.exit_ts) - _parse_ts(t.entry_ts)
        hold_mins.append(dt.total_seconds() / 60.0)
    
    avg_hold = (sum(hold_mins) / len(hold_mins)) if hold_mins else 0.0

    return Metrics(
        trades=len(trade_rows),
        round_trips=rt_n,
        wins=wins_n,
        losses=losses_n,
        win_rate=win_rate,
        avg_win=float(avg_win) * reporting_scale,
        avg_loss=float(avg_loss) * reporting_scale,
        profit_factor=float(profit_factor),
        expectancy=float(expectancy) * reporting_scale,
        max_drawdown=max_drawdown(equity) * reporting_scale,
        final_equity=float(final_equity) * reporting_scale,
        avg_hold_minutes=float(avg_hold),
        exits_by_reason=exits_by_reason,
        sharpe_ratio=float(sharpe_ann) # The new field
    )
