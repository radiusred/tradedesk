# tradedesk

A lightweight Python framework for building, running, and backtesting systematic trading strategies.

`tradedesk` provides the **infrastructure layer** for trading systems: data handling,
indicator management, strategy lifecycle orchestration, and backtesting.
It is intentionally opinionated about *structure*, but unopinionated about *strategy logic*.

This repository is designed to be reused across multiple trading projects.

---

## What tradedesk is (and is not)

**tradedesk is:**
- A framework for wiring together strategies, indicators, and data feeds
- A backtesting engine for candle- and tick-driven strategies
- A foundation for both live trading and historical simulation

**tradedesk is not:**
- A turnkey trading strategy
- A data downloader (see `tradedesk-dukascopy` for that)
- A broker-specific SDK

---

## Quick start

Install:

```bash
pip install tradedesk
```

Run a backtest from CSV candle data:

```python
from tradedesk.backtest import backtest_from_csv

results = backtest_from_csv(
    csv_path="EURUSD_5MIN.csv",
    strategy_cls=MyStrategy,
)

print(results.metrics)
```

The CSV format is intentionally simple and compatible with tools like
`tradedesk-dukascopy`.

---

## Core concepts

### Strategy lifecycle

A strategy typically follows this lifecycle:

1. Initialise indicators and subscriptions
2. Warm up using historical data
3. React to market events (ticks, candles)
4. Place and manage orders
5. Exit cleanly and report metrics

The framework enforces a clear separation between **signal generation**
and **execution mechanics**.

Take a look at some basic [examples](./docs/examples/) to see how to wire up to the framework, and have a read of the more comprehensive [strategy writing guide](./docs/STRATEGY_GUIDE.md) for more information.

---

### Indicators

Indicators are:
- Explicitly registered by strategies
- Warmed up deterministically
- Isolated per chart / timeframe

The framework ensures indicators are never used before they are ready.

---

### Backtesting

Backtests in `tradedesk` are:
- Event-driven (ticks or candles)
- Deterministic and reproducible
- Fast enough for iterative strategy development

Backtests produce:
- Trade lists
- Equity curves
- Summary metrics (win rate, drawdown, expectancy, etc.)

---

## Typical workflow

A common workflow using the `tradedesk` ecosystem:

1. Export historical data using `tradedesk-dukascopy`
2. Commit or archive the CSV + metadata
3. Develop and test strategies locally using `tradedesk`
4. Iterate rapidly using backtests
5. (Optionally) deploy the same strategy code live

---

## Design principles

- **Separation of concerns**: data, indicators, strategy logic, execution
- **Determinism**: identical inputs produce identical outputs
- **Explicitness**: no hidden global state
- **Testability**: core logic is unit-testable without live services

---

## Requirements

- Python 3.11+
- pandas, numpy (transitive dependencies)

---

## License

Apache 2.0. See `LICENSE` and `NOTICE` for details.
