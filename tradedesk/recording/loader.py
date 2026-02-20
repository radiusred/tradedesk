"""API for loading backtest trade data."""

import csv
from pathlib import Path

from .metrics import round_trips_from_fills


def load_trades_from_backtest(backtest_dir: Path) -> list[dict[str, str | float]]:
    """Load trade history from a backtest directory.

    Reads trades.csv and converts fills to round trip trades using the canonical
    round_trips_from_fills() function.

    Args:
        backtest_dir: Path to backtest results directory containing trades.csv

    Returns:
        List of trade dicts with keys: instrument, direction, entry_ts, exit_ts,
        entry_price, exit_price, size, pnl

    Raises:
        FileNotFoundError: If trades.csv doesn't exist in backtest_dir
        ValueError: If trades.csv is empty
    """
    trades_csv_path = backtest_dir / "trades.csv"

    if not trades_csv_path.exists():
        raise FileNotFoundError(
            f"trades.csv not found in {backtest_dir}. "
            "Ensure the directory contains a valid backtest."
        )

    # Read fills from trades.csv
    fills_dicts = []

    with open(trades_csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Support both 'instrument' and legacy 'epic' column names
            instrument = row.get("instrument") or row.get("epic", "")
            fills_dicts.append(
                {
                    "instrument": instrument,
                    "direction": row["direction"],
                    "timestamp": row["timestamp"],
                    "price": row["price"],
                    "size": row["size"],
                }
            )

    if not fills_dicts:
        raise ValueError(f"No trades found in {trades_csv_path}")

    # Convert fills to round trips using canonical function
    round_trips = round_trips_from_fills(fills_dicts)

    # Convert to simple dict format
    trades = []
    for trip in round_trips:
        trades.append({
            "instrument": trip.instrument,
            "direction": trip.direction.value,
            "entry_ts": trip.entry_ts,
            "exit_ts": trip.exit_ts,
            "entry_price": trip.entry_price,
            "exit_price": trip.exit_price,
            "size": trip.size,
            "pnl": trip.pnl,
        })

    return trades
