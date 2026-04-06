# Risk Management Guide

The `tradedesk.portfolio` and `tradedesk.execution` modules provide utilities for position sizing and position state tracking.

## Position Sizing

### ATR-Normalized Sizing

The `atr_normalised_size()` function calculates position size based on Average True Range (ATR):

```python
from tradedesk.portfolio import atr_normalised_size

# Calculate position size
size = atr_normalised_size(
    risk_per_trade=100.0,    # Amount of capital to risk
    atr=0.0050,              # Current ATR value
    atr_risk_mult=2.0,       # ATR multiplier for stop distance
    min_size=0.1,            # Minimum position size
    max_size=10.0,           # Maximum position size
)

# size = risk_per_trade / (atr * atr_risk_mult)
# size = 100.0 / (0.0050 * 2.0) = 10000.0
# Clamped to max_size: 10.0
```

### Formula

```
raw_size = risk_per_trade / (atr * atr_risk_mult)
final_size = clamp(raw_size, min_size, max_size)
```

- **risk_per_trade**: The amount of capital you're willing to risk on this trade
- **atr**: Current ATR value (measure of volatility)
- **atr_risk_mult**: How many ATRs away your stop loss is
- **min_size**: Minimum position size (broker/risk limits)
- **max_size**: Maximum position size (risk limits)

### Example Use Cases

#### Conservative Sizing

```python
# Risk 1% of $10,000 account = $100
# ATR = 0.0020, stop at 2x ATR

size = atr_normalised_size(
    risk_per_trade=100.0,
    atr=0.0020,
    atr_risk_mult=2.0,
    min_size=0.1,
    max_size=50.0,
)
# size = 100 / (0.0020 * 2.0) = 25000
# Clamped to max: 50.0
```

#### Adaptive Sizing

```python
# Adjust position size based on market volatility
from tradedesk.marketdata.indicators import ATR

atr_indicator = ATR(period=14)

# ... update indicator with candles ...

current_atr = atr_indicator.value()

if current_atr:
    size = atr_normalised_size(
        risk_per_trade=risk_amount,
        atr=current_atr,
        atr_risk_mult=2.0,
        min_size=min_trade_size,
        max_size=max_trade_size,
    )
```

## Position Tracking

### PositionTracker

The `PositionTracker` class maintains state for an open position:

```python
from tradedesk.execution import PositionTracker
from tradedesk.types import Direction

# Create tracker
position = PositionTracker()

# Open a position
position.open(
    direction=Direction.LONG,
    size=1.5,
    entry_price=1.1000
)

# Check position state
assert not position.is_flat()
assert position.direction == Direction.LONG
assert position.size == 1.5
assert position.entry_price == 1.1000
assert position.bars_held == 0
```

### Tracking Position Metrics

```python
from tradedesk import Candle

# Update with each new candle
candle = Candle(
    timestamp="1234567890000",
    open=1.1010,
    high=1.1050,
    low=1.1000,
    close=1.1030
)

# Track bars held
position.bars_held += 1

# Update Maximum Favorable Excursion (MFE)
position.update_mfe(candle)
print(f"MFE: {position.mfe_points} points")

# Calculate current P&L
pnl = position.current_pnl_points(current_price=1.1030)
print(f"Current P&L: {pnl} points")
```

### MFE/MAE Tracking

Maximum Favorable Excursion (MFE) tracks the best price movement:

```python
# For LONG positions: MFE = max(high - entry_price)
# For SHORT positions: MFE = max(entry_price - low)

position.open(Direction.LONG, size=1.0, entry_price=100.0)

candle1 = Candle(timestamp="1", open=100, high=105, low=99, close=103)
position.update_mfe(candle1)
assert position.mfe_points == 5.0  # 105 - 100

candle2 = Candle(timestamp="2", open=103, high=107, low=102, close=106)
position.update_mfe(candle2)
assert position.mfe_points == 7.0  # 107 - 100 (updated to new max)
```

### Complete Position Lifecycle

```python
from tradedesk.execution import PositionTracker
from tradedesk.types import Direction

class MyStrategy:
    def __init__(self):
        self.position = PositionTracker()

    async def on_entry_signal(self, direction: Direction, size: float, price: float):
        """Open a new position."""
        if not self.position.is_flat():
            return  # Already in a position

        self.position.open(direction, size, price)
        print(f"Entered {direction.value} at {price}")

    async def on_candle_close(self, candle):
        """Update position metrics."""
        if self.position.is_flat():
            return

        # Track bars
        self.position.bars_held += 1

        # Update MFE
        self.position.update_mfe(candle)

        # Calculate current P&L
        pnl = self.position.current_pnl_points(candle.close)

        print(f"Bars held: {self.position.bars_held}, "
              f"MFE: {self.position.mfe_points:.4f}, "
              f"P&L: {pnl:.4f}")

        # Check exit conditions
        if self.should_exit(pnl):
            await self.exit_position(candle.close)

    async def exit_position(self, price: float):
        """Close the position."""
        pnl = self.position.current_pnl_points(price)
        print(f"Exited at {price}, P&L: {pnl:.4f}, MFE: {self.position.mfe_points:.4f}")

        # Reset for next trade
        self.position.reset()

    def should_exit(self, current_pnl: float) -> bool:
        """Example exit logic."""
        # Stop loss
        if current_pnl < -50.0:
            return True

        # Take profit
        if current_pnl > 100.0:
            return True

        # Trailing stop (give back logic)
        if self.position.bars_held > 10 and self.position.mfe_points > 50.0:
            # Exit if we've given back 50% of MFE
            if current_pnl < self.position.mfe_points * 0.5:
                return True

        return False
```

## P&L Calculation

### Points-Based P&L

```python
# LONG position
entry = 1.1000
current = 1.1050
size = 2.0

pnl_points = (current - entry) * size  # 0.0050 * 2.0 = 0.01 points

# SHORT position
entry = 1.1000
current = 1.0950
size = 2.0

pnl_points = (entry - current) * size  # 0.0050 * 2.0 = 0.01 points
```

The `PositionTracker.current_pnl_points()` method handles this automatically:

```python
# LONG position
position.open(Direction.LONG, size=2.0, entry_price=1.1000)
pnl = position.current_pnl_points(current_price=1.1050)
assert pnl == pytest.approx(0.01)  # (1.1050 - 1.1000) * 2.0

# SHORT position
position.reset()
position.open(Direction.SHORT, size=2.0, entry_price=1.1000)
pnl = position.current_pnl_points(current_price=1.0950)
assert pnl == pytest.approx(0.01)  # (1.1000 - 1.0950) * 2.0
```

## Best Practices

### 1. Size to Your Risk Tolerance

```python
# Calculate risk per trade as % of account
account_size = 10000.0
risk_percent = 0.01  # 1%
risk_per_trade = account_size * risk_percent

size = atr_normalised_size(
    risk_per_trade=risk_per_trade,
    atr=current_atr,
    atr_risk_mult=2.0,
    min_size=0.1,
    max_size=50.0,
)
```

### 2. Use Reasonable ATR Multiples

- **Tight stops (1-1.5 ATR)**: Higher win rate, but more stop-outs
- **Medium stops (2-3 ATR)**: Balanced approach
- **Wide stops (>3 ATR)**: Lower win rate, but winners run longer

### 3. Set Position Limits

Always enforce min/max position sizes:

```python
# Broker minimums
min_size = 0.01

# Risk management maximums
# (e.g., never risk more than 5% of account on one trade)
max_size = calculate_max_size_from_risk(account_size, max_risk_pct=0.05)

size = atr_normalised_size(
    risk_per_trade=risk_amount,
    atr=atr,
    atr_risk_mult=mult,
    min_size=min_size,
    max_size=max_size,
)
```

### 4. Track Position Metrics

Use `PositionTracker` to analyze trade quality:

```python
# After exit
print(f"Trade held {position.bars_held} bars")
print(f"Max favorable: {position.mfe_points} points")
print(f"Final P&L: {final_pnl} points")
print(f"Efficiency: {final_pnl / position.mfe_points if position.mfe_points else 0:.2%}")
```

### 5. Handle Edge Cases

```python
# Zero or negative ATR
atr = get_current_atr()
if atr is None or atr <= 0:
    # Skip this trade or use default size
    return

# Very small ATR (would result in huge position)
if atr < min_atr_threshold:
    # Use minimum ATR to avoid oversizing
    atr = min_atr_threshold

size = atr_normalised_size(risk_per_trade, atr, ...)
```

## See Also

- [Indicator Guide](indicator_guide.md) - ATR and other indicators
- [Portfolio Guide](portfolio_guide.md) - Multi-instrument risk management
- [Strategy Guide](strategy_guide.md) - Integrating risk management in strategies

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) | [Contact](mailto:opensource@radiusred.uk)
