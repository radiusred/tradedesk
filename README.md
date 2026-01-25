# tradedesk

![CI Build](https://github.com/radiusred/tradedesk/actions/workflows/ci.yml/badge.svg)
[![PyPI Version](https://img.shields.io/pypi/v/tradedesk?label=PyPI)](https://pypi.python.org/pypi/tradedesk)

## What tradedesk is

`tradedesk` is a lightweight Python framework for developing, backtesting, and running systematic trading strategies across multiple data providers and execution environments.

It provides:

* A consistent strategy lifecycle
* Explicit separation between market data, strategy logic, and execution
* Deterministic [backtesting](./docs/backtesting_guide.md) with identical strategy code
* Live and DEMO execution via provider-specific clients

It is designed for research, validation, and controlled deployment of trading strategies rather than high-frequency or ultra-low-latency trading.

## What tradedesk is not

`tradedesk` is intentionally *not*:

* A portfolio management system
* A signal marketplace
* A turnkey trading bot
* A performance-optimised HFT engine

It REQUIRES the user to accept responsibility for strategy design, risk management, and operational controls.

## High-level architecture

At a high level, `tradedesk` consists of four layers:

1. **Providers** – Interfaces to external data/execution sources (e.g. IG, historical backtest data)
2. **Clients** – Concrete implementations that fetch data and place orders
3. **Strategies** – User-defined trading logic responding to market events
4. **Runner** – Orchestrates lifecycle, subscriptions, warmup, and shutdown

Market data flows from the provider into the strategy, which emits execution decisions back through the client.

## Core concepts

### Strategy lifecycle

A strategy progresses through the following phases:

1. Construction
2. Subscription registration
3. Warmup (optional but strongly recommended)
4. Live or replayed market data handling
5. Order execution
6. Graceful shutdown

### Subscriptions

Strategies explicitly declare their required data via subscriptions:

* **MarketSubscription** – Tick-level price updates (bid/offer)
* **ChartSubscription** – Aggregated candle data for a given timeframe

Only subscribed data is delivered to the strategy.

### Warmup

Warmup allows a strategy to request historical data before live execution begins in order to initialise indicator state and internal windows.

A strategy that does not warm up must be robust to partially initialised indicators and delayed signal readiness.

### Backtest vs live execution

The same strategy code can be run against:

* A live or DEMO provider
* A deterministic [backtest](./docs/backtesting_guide.md) client that replays historical data

Differences between environments are isolated to the client layer.

## Supported providers

* **IG** – Live and DEMO trading via REST and streaming APIs
* **BacktestClient** – Deterministic replay of historical data

Historical data acquisition is handled by a companion project:

* `tradedesk-dukascopy`

## Quick start

1. Install dependencies
2. Write or select a strategy
3. Choose a client ([backtest](./docs/backtesting_guide.md) or live)
4. Run via the `tradedesk` runner

Detailed tutorials are provided in the documentation guides listed below.

## Project status and guarantees

* APIs may evolve as the framework matures
* Backwards compatibility is maintained on a best-effort basis
* The framework prioritises correctness and clarity over performance

## Further reading

* `docs/indicators.md` – Indicator concepts and mathematical foundations
* `docs/strategy_writing_guide.md` – Step-by-step strategy tutorial
* `docs/backtesting_guide.md` – Methodology for rigorous backtesting

---

## License

Licensed under the Apache License, Version 2.0.
See: [https://www.apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0)

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred)
