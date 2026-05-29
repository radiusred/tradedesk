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
- External market datasets and parsers
- Portfolio orchestration and risk controls
- Trade recording, metrics, and reporting
- Research helpers for walk-forward validation and correlation checks
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
- `tradedesk.data_sources` for external datasets such as CFTC COT history
- `tradedesk.strategy` for strategy base classes and strategy-facing events
- `tradedesk.portfolio` for portfolio state, sizing, and risk policies
- `tradedesk.execution` for live execution adapters and order handling
- `tradedesk.execution.backtest` for simulated execution and replay
- `tradedesk.recording` for lifecycle events, trade records, and metrics
- `tradedesk.research` for walk-forward and correlation-gate helpers
- `tradedesk.ml` for optional feature engineering, labels, and walk-forward tooling

For a broader system map, see [docs/architecture.md](docs/architecture.md).

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

## External data sources

`tradedesk.data_sources` exposes loaders and parsers for datasets that sit
outside the live IG / Dukascopy execution paths.

Three free, no-auth macro feeds are supported:

- **FRED** — US rates (DFF, DGS3MO/2/10, T10Y2Y) and VIX from the St. Louis Fed
- **ECB** — EUR €STR, AAA government yield curve (3M–10Y) and Euribor from the
  ECB Data Portal
- **CFTC COT** — Commitment-of-Traders positioning for metals, energy, indices,
  10Y notes and CME EUR/JPY/GBP currency futures, including the TFF dealer,
  asset-manager and leveraged-funds buckets

Series are materialized to a Parquet lake under the existing market-data root
and loaded uniformly via `load_macro_series` / `load_macro_frame`:

```python
from tradedesk.data_sources import load_macro_series, load_macro_frame

rates = load_macro_frame("FRED", ["DGS2", "DGS10", "VIXCLS"])
estr = load_macro_series("ECB", "EUR_ESTR")
eur_cot = load_macro_series("CFTC", "EURUSD")
```

A `python -m tradedesk.data_sources.ingest` CLI refreshes the lake on demand or
on a weekly cron (idempotent; per-series failures are non-fatal).

See [docs/data_sources_guide.md](docs/data_sources_guide.md) for the full series
catalogue, the look-ahead semantics for CFTC release dates, and the lower-level
CFTC COT API (`CFTC_CONTRACTS`, `load_contract_history`, `download_cot_zip`,
`iter_cot_rows`, `cot_release_date`).

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
- [docs/data_sources_guide.md](docs/data_sources_guide.md)
- [docs/ml_guide.md](docs/ml_guide.md)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, quality gates,
and PR expectations.

## License

Licensed under the Apache License, Version 2.0.
See: [https://www.apache.org/licenses/LICENSE-2.0](https://www.apache.org/licenses/LICENSE-2.0)

Copyright 2026 [Radius Red Ltd.](https://www.radiusred.uk) |
[Contact](mailto:opensource@radiusred.uk)
