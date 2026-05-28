# Performance Metrics Guide

The `tradedesk.recording` module provides tools for analyzing trading strategy performance.

## Overview

The metrics module helps you:

- Reconstruct round-trip trades from fill history
- Calculate performance statistics (win rate, profit factor, drawdown, etc.)
- Derive equity curves from trade history
- Analyze trade quality and holding periods

## Quick Start

```python
from tradedesk.recording import compute_metrics

# Your trade fills
trade_rows = [
    {"timestamp": "2025-01-01T00:00:00Z", "instrument": "EURUSD", "direction": "BUY", "size": "1", "price": "1.1000"},
    {"timestamp": "2025-01-01T01:00:00Z", "instrument": "EURUSD", "direction": "SELL", "size": "1", "price": "1.1050"},
]

# Equity snapshots
equity_rows = [
    {"timestamp": "2025-01-01T00:00:00Z", "equity": "10000"},
    {"timestamp": "2025-01-01T01:00:00Z", "equity": "10050"},
]

# Compute all metrics
metrics = compute_metrics(
    equity_rows=equity_rows,
    trade_rows=trade_rows,
    reporting_scale=1.0
)

print(f"Win Rate: {metrics.win_rate:.1%}")
print(f"Profit Factor: {metrics.profit_factor:.2f}")
print(f"Max Drawdown: {metrics.max_drawdown:.2f}")
```

## Round Trip Reconstruction

Convert fill history into complete round-trip trades:

```python
from tradedesk.recording import round_trips_from_fills

fills = [
    {"instrument": "EURUSD", "direction": "BUY", "timestamp": "2025-01-01T10:00:00Z", "price": "1.1000", "size": "2"},
    {"instrument": "EURUSD", "direction": "SELL", "timestamp": "2025-01-01T11:00:00Z", "price": "1.1050", "size": "2"},
]

trips = round_trips_from_fills(fills)

trip = trips[0]
print(f"Instrument: {trip.instrument}")
print(f"Direction: {trip.direction}")  # "LONG"
print(f"Entry: {trip.entry_price} at {trip.entry_ts}")
print(f"Exit: {trip.exit_price} at {trip.exit_ts}")
print(f"P&L: {trip.pnl}")  # (1.1050 - 1.1000) * 2 = 0.01
```

### Trade Direction Logic

- **BUY fill when flat** → Opens LONG position
- **SELL fill when flat** → Opens SHORT position
- **Opposite fill when in position** → Closes position (creates round trip)

### Multiple Instruments

```python
fills = [
    {"instrument": "EURUSD", "direction": "BUY", "timestamp": "2025-01-01T10:00:00Z", "price": "1.1000", "size": "1"},
    {"instrument": "GBPUSD", "direction": "SELL", "timestamp": "2025-01-01T10:30:00Z", "price": "1.2500", "size": "1"},
    {"instrument": "EURUSD", "direction": "SELL", "timestamp": "2025-01-01T11:00:00Z", "price": "1.1050", "size": "1"},
    {"instrument": "GBPUSD", "direction": "BUY", "timestamp": "2025-01-01T11:30:00Z", "price": "1.2450", "size": "1"},
]

trips = round_trips_from_fills(fills)
# Returns 2 round trips: EURUSD LONG and GBPUSD SHORT
```

### Recorder Output Schema

`round_trips_from_fills()` only needs the canonical fill fields:

- `instrument`
- `direction`
- `timestamp`
- `price`
- `size`

`reason` is optional, but you should include it on exit fills when you want exit
reason analysis in the reconstructed trips and metrics output.

When you use the built-in recording subscriber, generated `trades.csv` rows also
carry the current cost decomposition fields:

- `raw_price`
- `spread_cost`
- `slippage_cost`
- `commission_cost`
- `financing_cost`
- `admin_cost`

Generated `round_trips.csv` then aggregates those costs to the round-trip level
and adds excursion columns (`mfe_points`, `mae_points`, `mfe_pnl`, `mae_pnl`)
when the run has the candle index needed to compute them.

### In-Memory Recording Identifiers

Recent recording updates also expose richer identifiers on the in-memory event
and record objects:

- `PositionOpenedEvent`: `strategy`, `position_id`
- `PositionClosedEvent`: `strategy`, `position_id`
- `TradeRecord`: `strategy`, `position_id`, `trade_id`

Those fields are useful for custom subscribers that need to correlate fills with
portfolio sleeves or a specific open/close lifecycle inside the same session.
The on-disk CSV artefacts are still the normalized fill and round-trip schema
described above, so if you need `strategy`, `position_id`, or `trade_id` today,
consume the events or `TradeRecord` instances directly rather than expecting
those identifiers in `trades.csv`.

## Performance Metrics

The `Metrics` dataclass contains comprehensive performance statistics:

```python
from tradedesk.recording import Metrics, compute_metrics

metrics = compute_metrics(equity_rows, trade_rows)

# Trade counts
print(f"Total fills: {metrics.trades}")
print(f"Round trips: {metrics.round_trips}")
print(f"Wins: {metrics.wins}")
print(f"Losses: {metrics.losses}")

# Win statistics
print(f"Win rate: {metrics.win_rate:.1%}")
print(f"Avg win: {metrics.avg_win:.2f}")
print(f"Avg loss: {metrics.avg_loss:.2f}")

# Performance ratios
print(f"Profit factor: {metrics.profit_factor:.2f}")
print(f"Expectancy: {metrics.expectancy:.4f}")

# Equity metrics
print(f"Final equity: {metrics.final_equity:.2f}")
print(f"Max drawdown: {metrics.max_drawdown:.2f}")

# Time analysis
if metrics.avg_hold_minutes:
    print(f"Avg hold: {metrics.avg_hold_minutes:.1f} minutes")

# Exit reasons
for reason, count in metrics.exits_by_reason.items():
    print(f"  {reason}: {count}")
```

### Metric Definitions

| Metric | Description | Formula |
|--------|-------------|---------|
| **win_rate** | Percentage of winning trades | wins / round_trips |
| **avg_win** | Average profit per winning trade | sum(wins) / count(wins) |
| **avg_loss** | Average loss per losing trade | sum(losses) / count(losses) |
| **profit_factor** | Ratio of gross profit to gross loss | sum(wins) / abs(sum(losses)) |
| **expectancy** | Expected value per trade | (win_rate × avg_win) + ((1 - win_rate) × avg_loss) |
| **max_drawdown** | Largest peak-to-trough decline | min(equity - peak_equity) |

### Special Cases

```python
# Only wins → profit_factor = inf
metrics = compute_metrics(
    equity_rows=[{"timestamp": "...", "equity": "10000"}, {"timestamp": "...", "equity": "10100"}],
    trade_rows=[
        {"instrument": "EURUSD", "direction": "BUY", "timestamp": "...", "price": "100", "size": "1"},
        {"instrument": "EURUSD", "direction": "SELL", "timestamp": "...", "price": "200", "size": "1"},
    ]
)
assert metrics.profit_factor == float("inf")
assert metrics.win_rate == 1.0

# Only losses → profit_factor = 0
# (opposite of above)

# No trades → all metrics zero/None
metrics = compute_metrics(equity_rows=[], trade_rows=[])
assert metrics.round_trips == 0
assert metrics.final_equity == 0.0
```

## Equity Curve Construction

Build an equity curve from round-trip P&L:

```python
from tradedesk.recording import round_trips_from_fills, equity_rows_from_round_trips

fills = [
    {"instrument": "EURUSD", "direction": "BUY", "timestamp": "2025-01-01T00:00:00Z", "price": "100", "size": "1"},
    {"instrument": "EURUSD", "direction": "SELL", "timestamp": "2025-01-01T01:00:00Z", "price": "110", "size": "1"},
    {"instrument": "EURUSD", "direction": "BUY", "timestamp": "2025-01-01T02:00:00Z", "price": "110", "size": "1"},
    {"instrument": "EURUSD", "direction": "SELL", "timestamp": "2025-01-01T03:00:00Z", "price": "115", "size": "1"},
]

trips = round_trips_from_fills(fills)
equity_rows = equity_rows_from_round_trips(trips, starting_equity=1000.0)

# Returns:
# [
#   {"timestamp": "2025-01-01T01:00:00Z", "equity": "1010"},  # 1000 + 10
#   {"timestamp": "2025-01-01T03:00:00Z", "equity": "1015"},  # 1010 + 5
# ]
```

## Reporting Scale

Use `reporting_scale` to convert between units (e.g., pips to currency):

```python
# Raw P&L in pips
metrics = compute_metrics(equity_rows, trade_rows, reporting_scale=1.0)
print(f"Avg win: {metrics.avg_win} pips")

# Convert to currency (e.g., 1 pip = $10)
metrics_usd = compute_metrics(equity_rows, trade_rows, reporting_scale=10.0)
print(f"Avg win: ${metrics_usd.avg_win}")

# Note: Only linear metrics are scaled
# Ratios (win_rate, profit_factor) remain unchanged
```

### Scaled vs Unscaled Metrics

**Scaled** (multiplied by reporting_scale):
- final_equity
- max_drawdown
- avg_win
- avg_loss
- expectancy

**Unscaled** (ratios/counts):
- trades
- round_trips
- wins
- losses
- win_rate
- profit_factor
- avg_hold_minutes

## Maximum Drawdown

Calculate maximum drawdown from an equity curve:

```python
from tradedesk.recording.metrics import max_drawdown

equity = [100, 110, 105, 95, 120, 115]
dd = max_drawdown(equity)  # -15.0 (peak 110, trough 95)

# Special cases
assert max_drawdown([]) == 0.0
assert max_drawdown([100, 101, 102]) == 0.0  # Monotonic up
```

## Complete Example: Strategy Analysis

```python
from tradedesk.recording import compute_metrics, round_trips_from_fills

class StrategyAnalyzer:
    def __init__(self):
        self.equity_snapshots = []
        self.trade_fills = []

    def record_equity(self, timestamp: str, value: float):
        """Record equity snapshot."""
        self.equity_snapshots.append({
            "timestamp": timestamp,
            "equity": str(value)
        })

    def record_fill(self, timestamp: str, instrument: str, direction: str,
                   size: float, price: float, reason: str = None):
        """Record trade fill."""
        fill = {
            "timestamp": timestamp,
            "instrument": instrument,
            "direction": direction,
            "size": str(size),
            "price": str(price),
        }
        if reason:
            fill["reason"] = reason
        self.trade_fills.append(fill)

    def analyze(self) -> dict:
        """Compute and return all metrics."""
        metrics = compute_metrics(
            equity_rows=self.equity_snapshots,
            trade_rows=self.trade_fills,
            reporting_scale=1.0
        )

        trips = round_trips_from_fills(self.trade_fills)

        return {
            "metrics": metrics,
            "round_trips": trips,
            "trade_count": len(self.trade_fills),
            "summary": {
                "win_rate": f"{metrics.win_rate:.1%}",
                "profit_factor": f"{metrics.profit_factor:.2f}",
                "expectancy": f"{metrics.expectancy:.4f}",
                "max_dd": f"{metrics.max_drawdown:.2f}",
                "avg_hold_hours": f"{metrics.avg_hold_minutes / 60:.1f}" if metrics.avg_hold_minutes else "N/A",
            }
        }

# Usage
analyzer = StrategyAnalyzer()

# Record trades
analyzer.record_fill("2025-01-01T10:00:00Z", "EURUSD", "BUY", 1.0, 1.1000)
analyzer.record_equity("2025-01-01T10:00:00Z", 10000.0)

analyzer.record_fill("2025-01-01T11:00:00Z", "EURUSD", "SELL", 1.0, 1.1050, reason="take_profit")
analyzer.record_equity("2025-01-01T11:00:00Z", 10050.0)

# Analyze
results = analyzer.analyze()
print(results["summary"])
```

## Best Practices

1. **Consistent timestamps**: Use ISO 8601 format for all timestamps

2. **Track exit reasons**: Include `"reason"` field in exit fills to analyze why trades closed

3. **Separate per-instrument**: Calculate metrics per instrument to identify best/worst performers

4. **Monitor during backtest**: Record metrics incrementally during backtesting, not just at the end

5. **Compare periods**: Calculate metrics for different time periods to detect strategy degradation

## See Also

- [Backtesting Guide](backtesting_guide.md) - Using metrics in backtests
- [Risk Management](risk_management.md) - Position sizing and tracking
- [Portfolio Guide](portfolio_guide.md) - Multi-instrument performance analysis

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://www.radiusred.uk) | [Contact](mailto:opensource@radiusred.uk)
