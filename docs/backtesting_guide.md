# Backtesting Strategies with tradedesk

---

## Purpose of this guide

This document describes a rigorous, repeatable methodology for backtesting trading strategies using the `tradedesk` framework.

It focuses on:
- correctness of methodology,
- limitations of historical simulation,
- disciplined interpretation of results,
- practical workflows that keep strategy code identical between backtest and live/DEMO.

It does **not** attempt to optimise strategies or suggest profitable configurations.

---

## What a backtest is (and is not)

A backtest is a controlled simulation of a strategy over historical market data.

A backtest can:
- validate that strategy logic behaves as intended,
- expose implementation bugs,
- provide comparative metrics between versions of the same strategy.

A backtest cannot:
- predict future performance,
- eliminate model risk,
- compensate for poor data quality or unrealistic execution assumptions.

Backtests are most useful as a **development instrument**: they tell you whether the implementation is coherent and whether performance is stable under controlled variation.

---

## Define a testable hypothesis

Before writing code or running a backtest, state a hypothesis in operational terms:

- **Premise**: what market behaviour are you exploiting?
- **Entry condition**: what observable data implies the premise holds?
- **Exit condition**: when is the premise no longer true?
- **Risk control**: how does the strategy fail closed?

Example (structure, not a recommendation):

- Premise: short bursts of momentum after volatility compression
- Entry: momentum oscillator crosses threshold while volatility is below a percentile
- Exit: time stop or ATR stop
- Risk control: no entry unless stop distance is computable; one position per epic

This matters because it prevents retrofitting explanations to results.

---

## Historical data requirements

Backtests are only as reliable as their input data.

Minimum requirements (non-negotiable if you want results to mean anything):
- **Timestamp integrity**: monotonic event ordering per instrument.
- **Timezone clarity**: a single, explicit timezone for all inputs.
- **Granularity match**: if the strategy is tick-driven, candle-only data implies approximation.
- **Gap visibility**: missing periods must be detectable (and ideally logged).

Recommended:
- Bid/offer (or mid + spread model) explicitly represented.
- Corporate action / contract roll awareness for instruments where it applies (indices/futures-style CFDs).

---

## Obtaining historical data

Historical data acquisition is intentionally separated from the core framework.

The companion project [`tradedesk-dukascopy`](https://github.com/radiusred/tradedesk-dukascopy) is responsible for:
- downloading historical data,
- caching and gap-filling,
- exporting to formats that are convenient for backtests.

This guide assumes you already have historical candles and/or tick/quote series available locally.

---

## Backtest architecture in tradedesk

Backtesting in `tradedesk` uses a dedicated client (`BacktestClient`) that replays data deterministically through the same strategy callbacks used in live execution.

Core properties:
- identical strategy code paths (same class, same callbacks),
- deterministic ordering,
- explicit configuration of what is replayed (candles, ticks/quotes, or both).

The practical implication: if you need special “backtest mode” logic inside a strategy, it should be isolated and justified.

---

## Event ordering and determinism

Two ordering rules matter in practice:

1. **No lookahead**: a strategy must not observe information that would not exist at that time (e.g., reading candle close values before the candle has closed).
2. **Stable replay**: given the same input series, results must be identical run-to-run.

If you cannot reproduce results exactly with the same inputs, treat the backtest as invalid until you can.

---

## Warmup in backtests

Indicators and windowed logic require warmup. Warmup must be handled explicitly:

- Warmup data is used to initialise state.
- Performance metrics must be computed on the evaluation window **after** warmup.
- Warmup length should exceed the largest lookback *plus* any additional state windows (e.g., regime windows, volume filters).

Typical approach:
- Load a longer historical period than you intend to evaluate.
- Use the early portion as warmup.
- Start recording metrics only after warmup completes.

If your strategy can trade before warmup completes, that is an implementation smell. Prefer fail-closed behaviour for entries.

---

## Candle-only backtesting

Candle-only datasets are common and useful, but impose constraints:

- Intrabar path is unknown.
- Stops cannot be simulated exactly without assumptions.
- Tick-driven indicators (e.g., MACD updated per tick) must be approximated.

Common approximation techniques:
- **Boundary synthetic ticks**: inject a synthetic tick at candle open (or close) to drive tick-based logic.
- **Stop checks via extremes**: approximate stop execution using candle high/low.

Be explicit: candle-only results are not “wrong”, but they are conditional on the approximation model. You must interpret them accordingly.

---

## Execution realism and modelling assumptions

Backtests simplify execution. You should choose assumptions deliberately and document them.

### Spread

At minimum:
- use bid/offer series if available, or
- apply a fixed spread model to a mid price.

### Slippage

If you do not model slippage, you are assuming:
- immediate fills at the observed price,
- no adverse selection.

That assumption is optimistic for market orders. If you want conservative tests, add slippage (even a small constant) or widen the spread.

### Stops

Stops are not guaranteed fills at the stop level in live markets, particularly during gaps. Candle-only stop modelling is especially optimistic/pessimistic depending on how you fill.

---

## A practical backtest workflow

This section shows a practical workflow with code. The examples assume:
- you already have candle history in memory or loaded from disk,
- you use `BacktestClient` to replay data,
- you run the strategy through the standard runner.

### Step 1: Assemble history inputs

`BacktestClient.from_history()` accepts a dictionary keyed by `(epic, timeframe)` with a list of candles.

Example (in-memory):

```python
from tradedesk.marketdata import Candle
from tradedesk.providers.backtest.client import BacktestClient

epic = "IX.D.FTSE.DAILY.IP"
timeframe = "5MINUTE"

candles = [
    Candle(timestamp="2025-01-02T08:00:00Z", open=7500, high=7502, low=7498, close=7501, volume=123, tick_count=0),
    # ...
]

history = {(epic, timeframe): candles}
client = BacktestClient.from_history(history)
```

If you are loading from disk, do that in your own code (or via `tradedesk-dukascopy`) and normalise into the same structure.

### Step 2: Configure the strategy spec

Backtests should use the same strategy class as live. Configure via `strategy_specs`:

```python
from tradedesk import run_strategies
from ig_trader.macd_trails_mfi_strategy import MacdTrailsMfiStrategy
from ig_trader.config import StrategyConfig
from ig_trader.epic_state import EpicState

def client_factory():
    return client

strategy_specs = [
    (
        MacdTrailsMfiStrategy,
        {
            "epics": [epic],
            "timeframe": timeframe,
            "size": 1.0,
            # Backtest knobs if required (example: candle-only approximations)
            "candle_only_backtest": True,
            "candle_only_spread": 1.0,
            "state_factory": lambda e: EpicState(
                epic=e,
                config=StrategyConfig(max_hold_bars=6, atr_stop_mult=2.0),
            ),
        },
    )
]
```

### Step 3: Run deterministically

```python
run_strategies(
    strategy_specs=strategy_specs,
    client_factory=client_factory,
    log_level="INFO",
)
```

Because `BacktestClient` is deterministic, repeated runs with identical inputs should produce identical trades and metrics.

---

## Measuring outcomes

Backtests require a consistent metrics interface. The goal is not “one metric”, but a coherent set:

- number of trades / round trips,
- win rate and payoff asymmetry,
- profit factor and expectancy,
- maximum drawdown and final equity,
- average hold time (if meaningful for your strategy).

### Record metrics before you run

Define what you will compare **before** executing the run:
- baseline version vs change,
- one parameter changed at a time,
- fixed evaluation window.

If you decide what to measure after you see results, you are already drifting toward overfitting.

---

## Sensitivity analysis

A strategy that only works for one instrument, one timeframe, and one specific parameter set is likely brittle.

Minimum sensitivity checks:
- adjacent timeframes (e.g., 5m vs 10m),
- related instruments (e.g., FTSE vs US500),
- small parameter perturbations (±10–20%).

If performance collapses under small perturbations, treat the “edge” as unproven.

---

## Common backtesting pitfalls

### Lookahead bias

Using candle close values to decide trades “inside” the candle.

Mitigation:
- ensure entry signals only use information available at that decision time,
- enforce strict event ordering (ticks first, candle close after).

### Data snooping / overfitting

Repeatedly tuning parameters until performance looks good.

Mitigation:
- out-of-sample evaluation windows,
- predefined metrics and acceptance criteria,
- sensitivity analysis.

### Silent data gaps

Missing periods that are interpreted as “no movement”.

Mitigation:
- gap detection in the data pipeline,
- explicit handling of missing ranges.

---

## From backtest to DEMO

Treat DEMO as a separate validation stage:
- event ordering differences (streaming vs replay),
- broker execution constraints (minimum sizes, rejections),
- connectivity/transient failure handling.

A strategy that backtests well but fails basic invariants in DEMO is not ready for live deployment, regardless of backtest performance.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred)
