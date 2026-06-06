# Portfolio Management Guide

The `tradedesk.portfolio` module provides a framework for managing multiple instruments with automated risk allocation.

## Core Concepts

### Portfolio Runner

The `PortfolioRunner` is a client-agnostic orchestrator that:

- Manages multiple strategies across different instruments
- Dynamically allocates risk based on regime activity
- Routes candle events to the appropriate strategies

```python
from tradedesk.portfolio import PortfolioRunner, EqualSplitRiskPolicy, Instrument

# Create strategies for different instruments
strategies = {
    Instrument("EURUSD"): eurusd_strategy,
    Instrument("GBPUSD"): gbpusd_strategy,
    Instrument("USDJPY"): usdjpy_strategy,
}

# Create portfolio runner with risk policy
runner = PortfolioRunner(
    strategies=strategies,
    policy=EqualSplitRiskPolicy(portfolio_risk_budget=100.0),
    default_risk_per_trade=10.0,
)

# Process candle events
await runner.on_candle_close(candle_event)
```

### Portfolio Strategy Protocol

Strategies managed by the portfolio must implement the `PortfolioStrategy` protocol with a **two-phase lifecycle**:

```python
from tradedesk.portfolio import PortfolioStrategy, Instrument
from tradedesk.marketdata.events import CandleClosedEvent

class MyStrategy:
    def __init__(self, instrument: str):
        self.instrument = Instrument(instrument)
        self._risk_per_trade = 10.0
        self._regime_active = False

    def set_risk_per_trade(self, value: float) -> None:
        """Called by PortfolioRunner between phase 1 and 2."""
        self._risk_per_trade = value

    def is_regime_active(self) -> bool:
        """Return True if strategy's regime is active."""
        return self._regime_active

    async def update_state(self, event: CandleClosedEvent) -> None:
        """Phase 1: Update indicators and regime state.

        Do NOT make trading decisions here. This happens before
        risk allocation, so risk_per_trade may not be current.
        """
        # Update indicators, regime filters, position tracking
        candle = event.candle
        # ... update logic ...
        self._regime_active = self._check_regime(candle)

    async def evaluate_signals(self) -> None:
        """Phase 2: Evaluate signals and execute trades.

        Make trading decisions here. This happens after risk
        allocation, so risk_per_trade is current and correct.
        """
        # Check entry/exit conditions and place orders
        # ... trading logic using self._risk_per_trade ...
        pass
```

## Risk Allocation Policies

### Equal Split Policy

The `EqualSplitRiskPolicy` divides a fixed portfolio risk budget equally across all active sleeves:

```python
from tradedesk.portfolio import EqualSplitRiskPolicy, Instrument

policy = EqualSplitRiskPolicy(portfolio_risk_budget=100.0)

# If 2 sleeves are active, each gets 50.0
active = {
    "Momentum_EURUSD": Instrument("EURUSD"),
    "Reversion_GBPUSD": Instrument("GBPUSD"),
}
allocation = policy.allocate(active)
# Returns: {"Momentum_EURUSD": 50.0, "Reversion_GBPUSD": 50.0}

# If 0 sleeves are active, returns empty dict
allocation = policy.allocate({})
# Returns: {}
```

### Custom Policies

You can create custom risk allocation policies:

```python
from dataclasses import dataclass
from typing import Mapping
from tradedesk.portfolio.types import Instrument

@dataclass(frozen=True)
class VolatilityWeightedPolicy:
    """Allocate more risk to less volatile instruments."""
    portfolio_risk_budget: float
    volatilities: dict[Instrument, float]

    def allocate(self, active_sleeves: Mapping[str, Instrument]) -> Mapping[str, float]:
        if not active_sleeves:
            return {}

        # Calculate inverse volatility weights
        inv_vols = {
            sleeve: 1.0 / self.volatilities[instrument]
            for sleeve, instrument in active_sleeves.items()
        }
        total_weight = sum(inv_vols.values())

        # Allocate proportionally
        return {
            sleeve: (inv_vols[sleeve] / total_weight) * self.portfolio_risk_budget
            for sleeve in active_sleeves
        }
```

## Risk Allocation Flow

The `PortfolioRunner` uses a **three-phase lifecycle** for each candle:

### Phase 1: Update State
```python
await strategy.update_state(event)
```
- Strategy updates indicators, regime filters, position tracking
- Regime state may change during this phase
- **No trading decisions** are made yet

### Phase 2: Apply Risk Budgets
```python
self._apply_risk_budgets()
```
- Check which strategies have active regimes (`is_regime_active()`)
- Apply policy to allocate risk across active sleeves
- Call `set_risk_per_trade()` on each strategy
- **Inactive strategies** receive `default_risk_per_trade`

### Phase 3: Evaluate Signals
```python
await strategy.evaluate_signals()
```
- Strategy evaluates entry/exit conditions
- **Trading decisions** use the correct, current risk allocation
- Orders are placed based on freshly allocated risk

**Why this matters**: When a regime activates, the risk allocation updates **before** the strategy makes trading decisions. This ensures the first trade uses the correct allocation, not a stale value.

## Complete Example

```python
from tradedesk.portfolio import (
    PortfolioRunner,
    EqualSplitRiskPolicy,
    Instrument,
    SleeveId,
)
from tradedesk import Candle
from tradedesk.marketdata.events import CandleClosedEvent

class SimpleStrategy:
    def __init__(self, instrument: str, threshold: float):
        self.instrument = Instrument(instrument)
        self.threshold = threshold
        self._risk_per_trade = 10.0
        self._regime_active = False
        self._price_history = []
        self._current_candle = None

    def set_risk_per_trade(self, value: float) -> None:
        self._risk_per_trade = value

    def is_regime_active(self) -> bool:
        return self._regime_active

    async def update_state(self, event: CandleClosedEvent) -> None:
        """Phase 1: Update indicators and regime state."""
        self._current_candle = event.candle
        self._price_history.append(event.candle.close)

        # Simple volatility regime: activate if price moves > threshold
        if len(self._price_history) >= 2:
            move = abs(event.candle.close - self._price_history[-2])
            self._regime_active = move > self.threshold

    async def evaluate_signals(self) -> None:
        """Phase 2: Make trading decisions with correct risk allocation."""
        if not self._regime_active or not self._current_candle:
            return

        # Entry logic using self._risk_per_trade (which is now current)
        # ... trading decisions here ...

# Create strategies
strategies = {
    SleeveId("Simple_EURUSD"): SimpleStrategy("EURUSD", threshold=0.001),
    SleeveId("Simple_GBPUSD"): SimpleStrategy("GBPUSD", threshold=0.002),
}

# Create runner
runner = PortfolioRunner(
    strategies=strategies,
    policy=EqualSplitRiskPolicy(portfolio_risk_budget=100.0),
    default_risk_per_trade=50.0,
)

# Process events
candle = Candle(timestamp="1234567890000", open=1.1000, high=1.1050,
                low=1.0950, close=1.1020)
await runner.on_candle_close(
    CandleClosedEvent(
        instrument=Instrument("EURUSD"),
        timeframe="15MINUTE",
        candle=candle,
    )
)
```

## Best Practices

1. **Set appropriate default risk**: The `default_risk_per_trade` should be large enough for strategies to operate if they activate independently.

2. **Monitor active count**: Track how many sleeves are typically active to size your `portfolio_risk_budget` appropriately.

3. **Implement clear regime logic**: The `is_regime_active()` method should have clear, testable conditions.

4. **Test policies**: Write tests for your custom policies to ensure they behave correctly with 0, 1, or many active sleeves.

5. **Handle edge cases**: Consider what happens when all strategies are inactive, or when one strategy dominates.

## Load-Bearing Invariants

`PortfolioRunner` supports multiple sleeves on the same instrument, but a few
important assumptions sit just below that surface:

- Risk allocation is keyed by `SleeveId`, not by instrument. Two AUDCAD sleeves
  with active regimes each receive their own budget slice.
- Event fan-out is instrument-based. When an AUDCAD candle closes, every AUDCAD
  sleeve runs `update_state()`, then the portfolio re-allocates risk, then
  every AUDCAD sleeve runs `evaluate_signals()`.
- The ordering is intentional and fragile: regime changes discovered during
  `update_state()` affect the very next `evaluate_signals()` call on that same
  candle because risk allocation happens between the two phases.

## Interaction With Execution And Recording

The portfolio layer is sleeve-aware, but the default execution and recording
paths are still instrument-netted:

- `PortfolioRunner` can host two sleeves on one instrument.
- The stock backtest client keeps one netted position per instrument.
- The built-in `RecordingSubscriber` pairs opens and closes by instrument when
  reconstructing fills.

That combination is safe when same-instrument sleeves do not maintain
overlapping independent live positions. If your runtime needs true parallel
same-instrument position lifecycles, you need custom execution and recording
plumbing keyed by `position_id` and `strategy`; the default recorder and CSV
metrics path are not a sleeve-aware trade-matching engine.

## See Also

- [Strategy Guide](strategy_guide.md) - Building trading strategies
- [Risk Management](risk_management.md) - Position sizing utilities
- [Backtesting Guide](backtesting_guide.md) - Testing portfolio strategies

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://www.radiusred.uk) | [Contact](mailto:opensource@radiusred.uk)
