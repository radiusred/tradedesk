![banner](https://i.ibb.co/Kc5C88gp/tradedesk-banner.webp)

# tradedesk

![CI Build](https://github.com/radiusred/tradedesk/actions/workflows/ci.yml/badge.svg)
[![PyPI Version](https://img.shields.io/pypi/v/tradedesk?label=PyPI)](https://pypi.python.org/pypi/tradedesk)

`tradedesk` is an event-driven trading framework for building, running, and
evaluating systematic strategies across backtesting and live broker
environments.

## What it provides

- Event-driven strategy execution
- Shared strategy model across backtest and live runs
- Market data aggregation and indicators
- Portfolio orchestration and risk controls
- Trade recording, metrics, and reporting
- Optional machine-learning helpers in `tradedesk.ml`

The design goal is portability: strategies react to framework events rather
than broker-specific implementations, so the same strategy code can move between
backtest and live execution with minimal runtime wiring changes.

## Installation

Python 3.11+ is required.

Install the published package:

```bash
pip install tradedesk
```

Install the optional machine-learning dependencies:

```bash
pip install 'tradedesk[ml]'
```

For local development:

```bash
pip install -e '.[dev]'
```

## Architecture at a glance

The public package is organized into a small set of domains:

- `tradedesk.marketdata` for market events, subscriptions, aggregation, and indicators
- `tradedesk.strategy` for strategy base classes and strategy-facing events
- `tradedesk.portfolio` for portfolio state, sizing, and risk policies
- `tradedesk.execution` for live execution adapters and order handling
- `tradedesk.execution.backtest` for simulated execution and replay
- `tradedesk.recording` for lifecycle events, trade records, and metrics
- `tradedesk.ml` for optional feature engineering, labels, and walk-forward tooling

For a broader system map, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Runtime model

Typical flow:

1. Market data arrives as ticks or candles.
2. Aggregation updates candle streams.
3. Strategies react in callbacks such as `on_price_update(...)` or
   `on_candle_close(...)`.
4. Strategies request orders through the execution layer.
5. Portfolio and execution components apply gates, place or simulate orders,
   and emit lifecycle events.
6. Recording subscribers capture fills, positions, equity, and metrics.

## Backtesting

Backtests use the same event model as live sessions.

- `BacktestClient.from_dukascopy_cache(...)` loads Dukascopy-backed history
- `run_backtest(...)` runs the event loop and recording pipeline
- Strategy, portfolio, and recording components behave the same way they do in
  live sessions

See [docs/backtesting_guide.md](docs/backtesting_guide.md) for the current
cache-backed workflow.

## Live trading with IG

The IG integration provides REST-backed order execution, price streaming, and
position synchronisation while keeping strategy code unchanged.

Live runs use these environment variables:

- `IG_API_KEY`
- `IG_USERNAME`
- `IG_PASSWORD`
- `IG_ENVIRONMENT` (`DEMO` by default, or `LIVE`)
- `IG_ACCOUNT_ID` for strategies that create tick-level `MarketSubscription`
  items

Example:

```bash
IG_API_KEY=... \
IG_USERNAME=... \
IG_PASSWORD=... \
IG_ENVIRONMENT=DEMO \
IG_ACCOUNT_ID=... \
python your_live_runner.py
```

`tradedesk` handles the short-lived IG session headers (`CST` and
`X-SECURITY-TOKEN`) during authentication. They are not configured manually.

When live sessions request historical candles from IG, account-level
historical-data limits are surfaced as a dedicated failure mode so embedding
runtimes can back off or alert explicitly.

## Machine learning support

`tradedesk.ml` is optional and installs behind the `[ml]` extra. It provides:

- Feature engineering over OHLC(V) and optional bid/ask data
- Label generation helpers
- Walk-forward cross-validation utilities
- Model wrappers and strategy integration points

See [docs/ml_guide.md](docs/ml_guide.md) for the ML overview and
[docs/ml_labels_guide.md](docs/ml_labels_guide.md) for label-specific details.

## Documentation

Start with:

- [docs/strategy_guide.md](docs/strategy_guide.md)
- [docs/backtesting_guide.md](docs/backtesting_guide.md)
- [docs/portfolio_guide.md](docs/portfolio_guide.md)
- [docs/risk_management.md](docs/risk_management.md)
- [docs/indicator_guide.md](docs/indicator_guide.md)
- [docs/aggregation_guide.md](docs/aggregation_guide.md)
- [docs/metrics_guide.md](docs/metrics_guide.md)
- [docs/settings.md](docs/settings.md)
- [docs/operational_resilience.md](docs/operational_resilience.md)
- [docs/crash-recovery.md](docs/crash-recovery.md)
- [docs/ml_guide.md](docs/ml_guide.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, quality gates,
and PR expectations.

## License

Licensed under the Apache License, Version 2.0.
See: [https://www.apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0)

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) |
[Contact](mailto:opensource@radiusred.uk)
