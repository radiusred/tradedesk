# Candle Aggregation Guide

The `tradedesk.aggregation` module provides time-bucketing candle aggregation for converting base-period candles into higher timeframes.

## Overview

`CandleAggregator` converts fast candles (e.g., 1-minute) into slower timeframes (e.g., 15-minute) using wall-clock time bucketing:

- **Time-aligned**: Buckets align to UTC time boundaries (not count-based)
- **Multi-instrument**: One aggregator can handle multiple instruments concurrently
- **Gap-tolerant**: Missing base candles don't break aggregation
- **Memory-efficient**: Only stores current bucket state per instrument

## Basic Usage

```python
from tradedesk.aggregation import CandleAggregator
from tradedesk import Candle

# Create aggregator for 15-minute candles from 5-minute base period
agg = CandleAggregator(target_period="15MINUTE", base_period="5MINUTE")

# Process base candles
base_candles = [
    Candle(timestamp="1704067200000", open=1.10, high=1.11, low=1.09, close=1.105),  # 00:00:00
    Candle(timestamp="1704067500000", open=1.105, high=1.12, low=1.10, close=1.115), # 00:05:00
    Candle(timestamp="1704067800000", open=1.115, high=1.13, low=1.11, close=1.125), # 00:10:00
    Candle(timestamp="1704068100000", open=1.125, high=1.14, low=1.12, close=1.135), # 00:15:00 (triggers)
]

instrument = "EURUSD"

# Process each base candle
result = agg.update(instrument=instrument, candle=base_candles[0])  # None (accumulating)
result = agg.update(instrument=instrument, candle=base_candles[1])  # None (accumulating)
result = agg.update(instrument=instrument, candle=base_candles[2])  # None (accumulating)
result = agg.update(instrument=instrument, candle=base_candles[3])  # Candle (bucket rolled!)

# result is now the aggregated 15-minute candle
assert result.timestamp == "1704067200000"  # Bucket start time
assert result.open == 1.10   # First open
assert result.high == 1.13   # Highest high
assert result.low == 1.09    # Lowest low
assert result.close == 1.125 # Last close
```

## Choosing Base Periods

Use `choose_base_period()` to automatically select an appropriate base period for your broker:

```python
from tradedesk.aggregation import choose_base_period

# Default: Uses common broker periods (SECOND, 1MINUTE, 5MINUTE, HOUR)
base = choose_base_period("15MINUTE")  # Returns "5MINUTE"
base = choose_base_period("7MINUTE")   # Returns "1MINUTE"
base = choose_base_period("HOUR")      # Returns "HOUR"

# Custom broker periods
broker_periods = ["1MINUTE", "5MINUTE", "15MINUTE", "1HOUR"]
base = choose_base_period("30MINUTE", supported_periods=broker_periods)  # Returns "15MINUTE"
```

### Selection Logic

The function prefers larger base periods when possible:
1. If target is exactly HOUR → use HOUR
2. If target is divisible by 5 minutes and ≥ 5 minutes → use 5MINUTE
3. If target is divisible by 1 minute and ≥ 1 minute → use 1MINUTE
4. Otherwise → use SECOND

## Multiple Instruments

One aggregator instance can handle many instruments with independent state:

```python
agg = CandleAggregator(target_period="15MINUTE")

# Process different instruments
result_eur = agg.update(instrument="EURUSD", candle=eurusd_candle)
result_gbp = agg.update(instrument="GBPUSD", candle=gbpusd_candle)

# Each instrument has its own bucket state
```

## OHLCV Aggregation Rules

When combining base candles into an aggregated candle:

- **Open**: First open of the bucket
- **High**: Maximum of all highs
- **Low**: Minimum of all lows
- **Close**: Last close of the bucket
- **Volume**: Sum of all volumes
- **Tick Count**: Sum of all tick counts

## Time Bucket Alignment

Buckets are aligned to UTC time boundaries:

```python
# For 15MINUTE target:
# Bucket 1: 00:00:00 - 00:15:00
# Bucket 2: 00:15:00 - 00:30:00
# Bucket 3: 00:30:00 - 00:45:00
# etc.

# A candle at 00:17:23 falls in Bucket 2
# The aggregated candle is emitted when the first candle from Bucket 3 arrives
```

## Advanced Usage

### Custom Broker Periods

```python
# Cryptocurrency exchange with non-standard periods
crypto_periods = ["1MINUTE", "3MINUTE", "5MINUTE", "15MINUTE", "1HOUR", "4HOUR"]

agg = CandleAggregator(
    target_period="15MINUTE",
    supported_periods=crypto_periods
)
```

### Inspecting Aggregator State

```python
agg = CandleAggregator(target_period="15MINUTE", base_period="5MINUTE")

base_period, target_period, factor = agg.describe()
print(f"Aggregating {factor}x {base_period} candles into {target_period}")
# Output: Aggregating 3x 5MINUTE candles into 15MINUTE
```

### Resetting State

```python
# Reset aggregation state for a specific instrument
agg.reset(instrument="EURUSD")

# Useful when reconnecting or recovering from errors
```

## Handling Missing Candles

The aggregator is gap-tolerant:

```python
# If you receive candles at:
# 00:00:00, 00:05:00, [MISSING: 00:10:00], 00:15:00

# The aggregator will emit a bucket containing only the 2 received candles
# The OHLCV values represent the partial data
```

## Complete Example: Live Aggregation

```python
from tradedesk.aggregation import CandleAggregator

class LiveAggregationStrategy:
    def __init__(self, target_period: str):
        self.aggregator = CandleAggregator(target_period=target_period)

    async def on_base_candle(self, instrument: str, candle):
        """Called when a new base-period candle arrives."""
        # Try to aggregate
        aggregated = self.aggregator.update(instrument=instrument, candle=candle)

        if aggregated:
            # Bucket rolled! Process the aggregated candle
            await self.on_aggregated_candle(instrument, aggregated)

    async def on_aggregated_candle(self, instrument: str, candle):
        """Called when a full target-period candle is ready."""
        print(f"{instrument} {candle.timestamp}: O={candle.open} H={candle.high} "
              f"L={candle.low} C={candle.close}")
```

## Best Practices

1. **Choose appropriate base periods**: Smaller base periods give more granular aggregation but require more frequent updates.

2. **Handle None returns**: `update()` returns `None` while accumulating. Always check before processing.

3. **Align with broker capabilities**: Use `choose_base_period()` to ensure compatibility with your broker's available periods.

4. **Test with gaps**: Your strategy should handle missing base candles gracefully.

5. **Monitor bucket counts**: In testing, verify you're getting the expected number of base candles per bucket.

## Common Patterns

### Building Multiple Timeframes

```python
# Create aggregators for multiple timeframes
agg_15m = CandleAggregator(target_period="15MINUTE")
agg_1h = CandleAggregator(target_period="HOUR")

async def on_base_candle(instrument: str, candle):
    # Feed to both aggregators
    c15 = agg_15m.update(instrument=instrument, candle=candle)
    c1h = agg_1h.update(instrument=instrument, candle=candle)

    if c15:
        await process_15min_candle(c15)
    if c1h:
        await process_1hour_candle(c1h)
```

### Backtesting with Aggregation

```python
# Backtest using historical 1-minute candles, strategy trades on 15-minute
agg = CandleAggregator(target_period="15MINUTE", base_period="1MINUTE")

for minute_candle in historical_1min_candles:
    aggregated = agg.update(instrument="EURUSD", candle=minute_candle)
    if aggregated:
        await strategy.on_candle_close(aggregated)
```

## See Also

- [Strategy Guide](strategy_guide.md) - Using aggregated candles in strategies
- [Backtesting Guide](backtesting_guide.md) - Backtesting with multiple timeframes

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred)
