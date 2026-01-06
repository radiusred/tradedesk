# Writing a Strategy with tradedesk

---

## Purpose of this guide

This guide is a practical, end-to-end tutorial for implementing trading strategies using the `tradedesk` framework.

It assumes:
- strong general programming ability,
- basic familiarity with Python,
- little or no prior experience with systematic trading frameworks.

The goal is to teach **correct strategy construction**, not trading theory or profitability.

---

## Mental model: what a strategy actually is

In `tradedesk`, a strategy is a **pure event-driven component**.

It:
- subscribes to market data,
- reacts to ordered events,
- updates internal state,
- emits execution decisions.

It does *not*:
- own capital,
- manage portfolios,
- retry failed orders,
- smooth over data or execution problems.

If something fails, the strategy must fail *closed*.

---

## Strategy lifecycle (concrete view)

A strategy progresses through these phases:

1. **Construction**
2. **Subscription registration**
3. **Warmup**
4. **Live / replayed execution**
5. **Shutdown**

Each phase has different constraints and failure modes.

---

## Construction and parameters

Construction is where you:
- define immutable parameters,
- declare required subscriptions,
- initialise per-instrument state.

A common anti-pattern is allowing parameters to change mid-run. Avoid this.

### Example: minimal constructor

```python
class MyStrategy(BaseStrategy):
    def __init__(
        self,
        client,
        *,
        epics: list[str],
        timeframe: str,
        size: float,
        state_factory,
    ):
        self.epics = list(epics)
        self.timeframe = timeframe
        self.size = float(size)

        subs = []
        for epic in self.epics:
            subs.append(MarketSubscription(epic))
            subs.append(ChartSubscription(epic, self.timeframe))

        super().__init__(client, subscriptions=subs)

        self.states = {epic: state_factory(epic) for epic in self.epics}
```

At this point:
- no network calls should occur,
- no assumptions about market state should be made.

---

## Subscriptions and data guarantees

A strategy will **only** receive events it explicitly subscribes to.

This is deliberate:
- it makes data dependencies explicit,
- it prevents accidental coupling to provider behaviour,
- it simplifies testing.

If your logic depends on candle closes, you *must* subscribe to chart data.

---

## Event callbacks and ordering

Strategies respond to events via callbacks.

The two most important are:

```python
async def on_price_update(self, market_data: MarketData): ...
async def on_candle_close(self, candle_close: CandleClose): ...
```

Ordering guarantees:
- ticks arrive before the candle close they contribute to,
- candle close is final and immutable.

Your strategy must not infer future candles or prices.

---

## Where state should live (important)

Do **not** store mutable trading state directly on the strategy.

Instead:
- create a per-epic state object,
- store indicators, windows, and position state there,
- keep the strategy as a coordinator.

This enables:
- clean unit testing,
- deterministic backtests,
- multi-epic safety.

---

## Warmup: why and how

Warmup exists to solve a real problem:
- indicators require history,
- early values are unstable,
- first live events are not representative.

### Enabling warmup

```python
def warmup_enabled(self) -> bool:
    return True
```

### Fetching history

Override `warmup_from_provider()` if you manage indicators yourself:

```python
async def warmup_from_provider(self):
    candles = await self.client.get_historical_candles(epic, timeframe, n)
    self.warmup_from_history({(epic, timeframe): candles})
```

### Critical rule

Warmup must **never place trades**.

If your warmup logic can accidentally trigger entries, your design is unsafe.

---

## Entry logic and invariants

Entries must enforce invariants explicitly.

Typical invariants:
- indicators are warmed and valid,
- stop distance is computable,
- no position already open.

Example:

```python
if signal == Signal.ENTRY_LONG:
    stop = state.compute_initial_stop(...)
    if stop is None:
        return  # fail closed

    await self.client.place_market_order(epic, "BUY", size)
    state.open_position(Direction.LONG, entry_price)
```

Skipping an entry is always preferable to entering in an undefined state.

---

## Exit logic and idempotency

Exit logic must tolerate repeated signals.

Rules:
- closing an already-closed position must be a no-op,
- exit signals may arrive multiple times,
- order placement must not duplicate state transitions.

This usually means:
- checking state before acting,
- updating state only after confirmed intent.

---

## Candle-only backtests (practical)

When tick data is unavailable:
- you must approximate tick-driven logic,
- results will be conditional.

Common approach:
- inject a synthetic tick at candle open,
- approximate stops via candle extremes.

Your strategy must make these approximations explicit.

---

## Testing strategies properly

A strategy should be testable without:
- a live broker,
- network access,
- time-based sleeps.

Recommended tests:
- unit tests for state transitions,
- deterministic backtest runs,
- explicit failure-mode tests (e.g. stop not ready).

If a strategy cannot be tested deterministically, it is not production-ready.

---

## Common implementation errors

- Trading before warmup completes
- Mixing indicator logic with execution
- Assuming events arrive at fixed intervals
- Allowing unprotected positions

These errors are architectural, not tactical.

---

## From strategy to backtest to DEMO

A healthy workflow is:

1. Implement strategy with explicit invariants
2. Unit test state and signals
3. Run deterministic backtests
4. Validate behaviour in DEMO
5. Only then consider live deployment

Backtests validate logic. DEMO validates integration.

---

## Worked example: a small but realistic strategy (happy path)

This section walks through a complete strategy implementation that is intentionally modest in scope but operationally realistic.

Design goals:
- **Candle-driven** signals (stable ordering)
- One instrument (`epic`) per state container
- Explicit **warmup** and **indicator readiness**
- Explicit entry invariants: **no entry without stop**
- Explicit exit logic: **time stop** and **ATR stop**
- Deterministic backtest execution using `BacktestClient`

This example is *not* a recommendation for live trading. It is a reference implementation pattern.

### Strategy definition

We will implement:
- a simple trend-following entry based on **EMA fast/slow crossover**,
- an ATR-based stop distance,
- a maximum holding period in bars.

The implementation uses two cooperating components:
- `EmaAtrState` – per-epic state (indicators + position tracking)
- `EmaAtrStrategy` – coordinator (subscriptions + execution)

### 1) State container

```python
from dataclasses import dataclass

from tradedesk.indicators.ema import EMA
from tradedesk.indicators.atr import ATR


@dataclass
class Position:
    direction: str  # "LONG" or "SHORT"
    entry_price: float
    entry_ts: str
    entry_bar_index: int
    stop_price: float


class EmaAtrState:
    def __init__(
        self,
        *,
        epic: str,
        ema_fast: int = 12,
        ema_slow: int = 26,
        atr_period: int = 14,
        atr_stop_mult: float = 2.0,
        max_hold_bars: int = 6,
    ) -> None:
        self.epic = epic
        self.ema_fast = EMA(period=ema_fast)
        self.ema_slow = EMA(period=ema_slow)
        self.atr = ATR(period=atr_period)

        self.atr_stop_mult = float(atr_stop_mult)
        self.max_hold_bars = int(max_hold_bars)

        self.bar_index = 0
        self.position: Position | None = None

        # Track last crossover state to avoid repeated entries.
        self._prev_fast_above_slow: bool | None = None

    def on_candle_close(self, candle) -> None:
        """Update indicators and bar count."""
        self.bar_index += 1

        # Candle-driven indicators
        self.ema_fast.update(candle.close)
        self.ema_slow.update(candle.close)
        self.atr.update(high=candle.high, low=candle.low, close=candle.close)

    def indicators_ready(self) -> bool:
        return (
            self.ema_fast.value is not None
            and self.ema_slow.value is not None
            and self.atr.value is not None
            and self.atr.value > 0
        )

    def compute_initial_stop(self, *, entry_price: float, direction: str) -> float | None:
        """Return a stop price, or None if ATR not ready."""
        if not self.indicators_ready():
            return None

        dist = float(self.atr.value) * self.atr_stop_mult
        if direction == "LONG":
            return entry_price - dist
        return entry_price + dist

    def entry_signal(self) -> str | None:
        """Return "LONG", "SHORT", or None."""
        if not self.indicators_ready() or self.position is not None:
            return None

        fast = float(self.ema_fast.value)
        slow = float(self.ema_slow.value)
        fast_above = fast > slow

        # First ready tick: initialise crossover state without trading.
        if self._prev_fast_above_slow is None:
            self._prev_fast_above_slow = fast_above
            return None

        # Crossover events only
        sig = None
        if fast_above and not self._prev_fast_above_slow:
            sig = "LONG"
        elif (not fast_above) and self._prev_fast_above_slow:
            sig = "SHORT"

        self._prev_fast_above_slow = fast_above
        return sig

    def exit_signal(self, candle) -> str | None:
        """Return "EXIT" or None."""
        if self.position is None:
            return None

        # Time stop
        held_bars = self.bar_index - self.position.entry_bar_index
        if held_bars >= self.max_hold_bars:
            return "EXIT"

        # ATR stop (candle-extreme approximation is acceptable for candle-only tests)
        if self.position.direction == "LONG" and candle.low <= self.position.stop_price:
            return "EXIT"
        if self.position.direction == "SHORT" and candle.high >= self.position.stop_price:
            return "EXIT"

        return None

    def open_position(self, *, direction: str, entry_price: float, entry_ts: str) -> None:
        stop = self.compute_initial_stop(entry_price=entry_price, direction=direction)
        if stop is None:
            raise RuntimeError("Attempted to open a position without a valid stop")

        self.position = Position(
            direction=direction,
            entry_price=float(entry_price),
            entry_ts=entry_ts,
            entry_bar_index=self.bar_index,
            stop_price=float(stop),
        )

    def close_position(self) -> None:
        self.position = None
```

Notes:
- `indicators_ready()` provides the single readiness check.
- `compute_initial_stop()` is the enforcement point: entries are invalid without ATR.
- `entry_signal()` emits only on crossover transitions, preventing repeated entries while trend persists.

### 2) Strategy coordinator

```python
import logging
from collections.abc import Callable

from tradedesk.strategy import BaseStrategy
from tradedesk.subscriptions import ChartSubscription
from tradedesk.marketdata import CandleClose

log = logging.getLogger(__name__)


class EmaAtrStrategy(BaseStrategy):
    def __init__(
        self,
        client,
        *,
        epics: list[str],
        timeframe: str = "5MINUTE",
        size: float = 1.0,
        state_factory: Callable[[str], EmaAtrState] | None = None,
    ) -> None:
        self.epics = list(epics)
        self.timeframe = timeframe
        self.size = float(size)

        subs = [ChartSubscription(epic, self.timeframe) for epic in self.epics]
        super().__init__(client, subscriptions=subs)

        self._state_factory = state_factory or (lambda e: EmaAtrState(epic=e))
        self.states: dict[str, EmaAtrState] = {epic: self._state_factory(epic) for epic in self.epics}

    def warmup_enabled(self) -> bool:
        return True

    async def warmup_from_provider(self) -> None:
        get_hist = getattr(self.client, "get_historical_candles", None)
        if not callable(get_hist):
            return

        history: dict[tuple[str, str], list] = {}
        for epic in self.epics:
            candles = await get_hist(epic, self.timeframe, 300)
            history[(epic, self.timeframe)] = candles or []

        self.warmup_from_history(history)

    def warmup_from_history(self, history: dict[tuple[str, str], list]) -> None:
        super().warmup_from_history(history)

        for (epic, tf), candles in history.items():
            if tf != self.timeframe:
                continue
            st = self.states.get(epic)
            if st is None:
                continue
            for c in candles:
                st.on_candle_close(c)

    async def on_candle_close(self, candle_close: CandleClose) -> None:
        await super().on_candle_close(candle_close)

        epic = candle_close.epic
        st = self.states.get(epic)
        if st is None:
            return

        candle = candle_close.candle
        st.on_candle_close(candle)

        # Exit has priority
        if st.exit_signal(candle) == "EXIT":
            if st.position is None:
                return

            if st.position.direction == "LONG":
                await self.client.place_market_order(epic, "SELL", self.size)
            else:
                await self.client.place_market_order(epic, "BUY", self.size)

            st.close_position()
            log.info("Exited %s", epic)
            return

        # Entry
        sig = st.entry_signal()
        if sig is None:
            return

        # Fail closed if stop cannot be computed (enforced in open_position).
        mid = float(candle.close)
        if sig == "LONG":
            await self.client.place_market_order(epic, "BUY", self.size)
            st.open_position(direction="LONG", entry_price=mid, entry_ts=str(candle.timestamp))
            log.info("Entered LONG %s", epic)
        else:
            await self.client.place_market_order(epic, "SELL", self.size)
            st.open_position(direction="SHORT", entry_price=mid, entry_ts=str(candle.timestamp))
            log.info("Entered SHORT %s", epic)
```

Notes:
- This example is candle-driven only, which simplifies event ordering.
- `warmup_from_provider()` warms state directly without relying on `register_indicator()`.

### 3) Run a deterministic backtest

```python
from tradedesk import run_strategies
from tradedesk.providers.backtest.client import BacktestClient

# history: {(epic, timeframe): [Candle, ...]}
client = BacktestClient.from_history(history)

def client_factory():
    return client

strategy_specs = [
    (
        EmaAtrStrategy,
        {
            "epics": ["IX.D.FTSE.DAILY.IP"],
            "timeframe": "5MINUTE",
            "size": 1.0,
            "state_factory": lambda e: EmaAtrState(
                epic=e,
                ema_fast=12,
                ema_slow=26,
                atr_period=14,
                atr_stop_mult=2.0,
                max_hold_bars=6,
            ),
        },
    )
]

run_strategies(
    strategy_specs=strategy_specs,
    client_factory=client_factory,
    log_level="INFO",
)

# BacktestClient typically exposes trades; capture them and compute metrics.
# The exact API depends on your client implementation.
print(getattr(client, "trades", []))
```

### 4) Minimal “happy path” tests

You should unit test the state container independently of the runner.

```python
from tradedesk.marketdata import Candle


def test_entry_requires_ready_indicators():
    st = EmaAtrState(epic="EPIC", ema_fast=3, ema_slow=5, atr_period=3)
    c = Candle(timestamp="0", open=1, high=1, low=1, close=1, volume=1, tick_count=0)

    # Not ready initially
    st.on_candle_close(c)
    assert st.entry_signal() is None


def test_open_position_requires_stop():
    st = EmaAtrState(epic="EPIC", ema_fast=3, ema_slow=5, atr_period=3)
    with pytest.raises(RuntimeError):
        st.open_position(direction="LONG", entry_price=1.0, entry_ts="0")
```

These tests are intentionally minimal: they verify the invariants that prevent undefined behaviour.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred)
