
![CI Build](https://github.com/radiusred/tradedesk/actions/workflows/ci.yml/badge.svg)
[![PyPI Version](https://img.shields.io/pypi/v/tradedesk?label=PyPI)](https://pypi.python.org/pypi/tradedesk)

# tradedesk

Contributing: [CONTRIBUTING.md](./CONTRIBUTING.md)

tradedesk is an event-driven trading framework for building, running,
and evaluating systematic trading strategies across both backtesting and live
broker environments.

It provides:

-   Event-based strategy execution
-   Unified backtest and live broker runtime model
-   Market data aggregation and indicator framework
-   Portfolio orchestration and risk management
-   Trade recording, metrics, and reporting

The framework is designed so that strategies react to events --- not
broker implementations --- enabling the same strategy code to run
unchanged in both backtest and live environments.


## Core Concepts

### Event-Driven Architecture

All major subsystems communicate via events:

-   Market data events (ticks, candles)
-   Strategy events (signals)
-   Execution events (order completions and broker fills)
-   Portfolio events (position updates and lifecycle transitions)
-   Recording events (trade lifecycle, equity, and reporting)

As a user, you primarily:

-   Implement a strategy that reacts to candle updates
-   Optionally subscribe to events for custom analytics or logging


## Architecture Overview

For a concise public map of the system, see ARCHITECTURE.md. tradedesk is built around an event-driven core that wires together market data, strategy logic, portfolio management, execution adapters (IG for live trading, Dukascopy-backed backtests), and a recording layer for metrics and reports. The public interfaces expose reusable building blocks (marketdata, strategy, portfolio, recording) with clear data-flow guarantees across backtest and live paths.

## Basic Strategy Structure

A strategy derives from the base strategy class and implements candle
handling logic.

Typical flow:

1.  Market data arrives (tick or candle)
2.  Aggregation produces candles
3.  Strategy receives `on_candle_close`
4.  Strategy emits order requests
5.  Execution layer processes orders
6.  Portfolio updates positions
7.  Recording captures trade lifecycle


## Running a Backtest

Backtesting uses the same event model as live trading.

High-level flow:

-   Dukascopy cache data is loaded via `BacktestClient.from_dukascopy_cache(...)`
-   `run_backtest(...)` drives the event loop and recording pipeline
-   Strategy code executes unchanged
-   Portfolio and recording operate identically to live mode

See `docs/backtesting_guide.md` for the current cache-backed workflow.


## Live Trading (IG)

The IG execution module provides:

-   REST client for order management
-   Streaming price integration
-   Position synchronization
-   Retry and resilience handling

Your strategy remains unchanged --- only the execution configuration
differs.

Orders placed through `request_order(...)` continue to flow through
`OrderExecutionHandler` in both backtest and live sessions. For clients such as
IG that do not publish their own position-open callbacks, tradedesk emits a
`PositionOpenedEvent` immediately after a confirmed opening fill. That keeps
recording subscribers and custom event consumers aligned across backtest,
DEMO, and LIVE runs without double-publishing for clients that already emit
their own lifecycle events.

### IG Credentials

Live IG runs read credentials from environment variables:

-   `IG_API_KEY` (required)
-   `IG_USERNAME` (required)
-   `IG_PASSWORD` (required)
-   `IG_ENVIRONMENT` (optional, defaults to `DEMO`, valid values are `DEMO` and `LIVE`)

Example:

```bash
IG_API_KEY=... \
IG_USERNAME=... \
IG_PASSWORD=... \
IG_ENVIRONMENT=DEMO \
python your_live_runner.py
```

`tradedesk` authenticates with IG and captures the short-lived session headers
(`CST` and `X-SECURITY-TOKEN`) from the login response automatically. You do not
configure those session tokens yourself.

When live sessions ask IG for historical candles, IG enforces a separate
account-level historical-data allowance. `tradedesk` treats that 403 response as
a distinct failure mode instead of retrying it as an authentication problem, so
embedding runtimes can back off or warn explicitly when warmup/history fetches
run out of quota.


## Portfolio & Risk

The portfolio subsystem:

-   Tracks positions
-   Applies risk policies
-   Reconciles fills
-   Emits portfolio events

Risk controls such as spread limits and portfolio-level order gates can reject
orders before broker submission.


## Recording & Reporting

The recording subsystem:

-   Tracks trades and equity curves
-   Computes excursions and performance metrics
-   Generates structured reports from position lifecycle events and fills

Users can subscribe to recording events for custom reporting pipelines.


## Typical Project Structure


    my_strategy/
        strategy.py
        run_backtest.py
        config.py


## Installation

Python 3.11+ is required.

Install the published package:

```bash
pip install tradedesk
```

For local development:

```bash
pip install -e '.[dev]'
```


## Documentation

See the `docs/` directory for:

-   Backtesting guide
-   Strategy guide
-   Portfolio guide
-   Indicator guide
-   Aggregation guide
-   Risk management guide
-   Metrics guide

Public package entry points are grouped under:

-   `tradedesk.marketdata`
-   `tradedesk.execution`
-   `tradedesk.execution.backtest`
-   `tradedesk.portfolio`
-   `tradedesk.recording`
-   `tradedesk.strategy`


tradedesk is designed for clarity, determinism, and event-level
transparency.

## See Also
- docs/backtesting_guide.md
- docs/strategy_guide.md
- docs/indicator_guide.md
- docs/crash-recovery.md

## Contributing

See CONTRIBUTING.md for guidelines on contributing to tradedesk.

## License

Licensed under the Apache License, Version 2.0.
See: [https://www.apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0)

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) | [Contact](mailto:opensource@radiusred.uk)
