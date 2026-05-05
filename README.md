

![banner](https://i.ibb.co/Kc5C88gp/tradedesk-banner.webp)

# tradedesk

![CI Build](https://github.com/radiusred/tradedesk/actions/workflows/ci.yml/badge.svg)
[![PyPI Version](https://img.shields.io/pypi/v/tradedesk?label=PyPI)](https://pypi.python.org/pypi/tradedesk)

**event-driven trading framework for building, running, and evaluating systematic trading strategies across both backtesting and live broker environments.**

Tradedesk provides:

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
-   `IG_ACCOUNT_ID` (required for strategies that construct tick-level `MarketSubscription` items)

Example:

```bash
IG_API_KEY=... \
IG_USERNAME=... \
IG_PASSWORD=... \
IG_ENVIRONMENT=DEMO \
IG_ACCOUNT_ID=... \
python your_live_runner.py
```

`tradedesk` authenticates with IG and captures the short-lived session headers
(`CST` and `X-SECURITY-TOKEN`) from the login response automatically. You do not
configure those session tokens yourself.

Strategies that subscribe to tick-level `MarketSubscription` updates on IG also
need to include the IG account identifier in each subscription item name. In
practice that usually means reading an `IG_ACCOUNT_ID` environment variable in
your strategy code and passing it as `account_id=...` when you construct each
`MarketSubscription`.

When live sessions ask IG for historical candles, IG enforces a separate
account-level historical-data allowance. `tradedesk` treats that 403 response as
a distinct failure mode instead of retrying it as an authentication problem, so
embedding runtimes can back off or warn explicitly when warmup/history fetches
run out of quota.

### IG spread gate and scalingFactor

When `OrderExecutionHandler` is configured with a spread limit, it checks the
current spread from the IG market snapshot before submitting each order. IG
returns bid and offer in broker-scaled units (for example, EURUSD bid≈11715.5
instead of 1.17155), so `tradedesk` divides by the `instrument.scalingFactor`
from the snapshot before computing the spread. Non-FX instruments (indices,
gold) use `scalingFactor=1`, leaving their prices unchanged. If you configure
`max_spread` thresholds, set them in the decimal price units your strategy uses
— the normalization is transparent.


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

To enable the optional machine-learning building blocks in `tradedesk.ml`
(feature engineering, walk-forward CV, XGBoost direction classifier — see
the Phase 6 sprint), install the `[ml]` extra:

```bash
pip install 'tradedesk[ml]'
```

For local development:

```bash
pip install -e '.[dev]'
```


## Machine Learning (`tradedesk.ml`)

The `tradedesk.ml` subpackage hosts the building blocks for ML-driven
strategies. ML extras (`xgboost`, `scikit-learn`, `joblib`) install via the
optional `[ml]` extra:

```bash
pip install 'tradedesk[ml]'
```

Phase 6 ships:

-   `FeatureBuilder` (`tradedesk.ml.features`) — feature engineering for
    1-minute OHLC(V) bid/ask bars. Built-in feature families:
    -   Lagged log returns over a fan of horizons (1, 5, 15, 60, 240 bars).
    -   Rolling realised volatility, skew and kurtosis of 1-min log returns.
    -   Time-of-day (cyclical sin/cos) and weekday.
    -   Outputs from a configurable indicator stack — by default the full
        `tradedesk.marketdata.indicators` set (ADX, ATR, Bollinger Bands,
        CCI, EMA, Keltner Channel, MACD, MFI, OBV, RSI, SMA, Stochastic,
        VWAP, Williams %R) driven by the same streaming indicator classes
        used in live trading, so backtest and live features are
        bit-identical.
    -   Microstructure ratios: body/range, upper/lower wick, plus
        bid/ask spread when those columns are present.

    Strict no-look-ahead — every column at bar `t` depends only on data up
    to and including `t`. Forward-return labels live in `tradedesk.ml.labels`
    and the walk-forward splitter in `tradedesk.ml.cv` enforces the
    embargo/purge that guards against label leakage at fold boundaries.

```python
from tradedesk.ml import FeatureBuilder, FeatureConfig

builder = FeatureBuilder()           # default stack + default config
features = builder.transform(bars)   # bars: DatetimeIndex, OHLC(V) [+ bid/ask]
```

Pass `indicators={...}` to swap or shrink the indicator stack and
`config=FeatureConfig(...)` to tune the rolling-window fan or toggle
optional feature families.

### Phase 6 quickstart — features → labels → model → walk-forward CV

The full Phase 6 stack lives behind `tradedesk[ml]` and is exposed via
`tradedesk.ml` and `tradedesk.strategy.MLDirectionStrategy`. The
end-to-end workflow:

```python
import pandas as pd
from tradedesk.ml import (
    FeatureBuilder, FeatureConfig,
    LabelConfig, forward_return_labels,
    WalkForwardConfig, WalkForwardSplitter, walk_forward_evaluate,
)
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig

# 1. Features --------------------------------------------------------------
builder = FeatureBuilder(config=FeatureConfig())
X = builder.transform(bars)   # bars indexed by UTC DatetimeIndex

# 2. Forward-return labels (binary up/down) --------------------------------
y_raw = forward_return_labels(bars, LabelConfig(horizon=15)).reindex(X.index)
valid = y_raw.notna()
X = X.loc[valid]
y = (y_raw.loc[valid] > 0).astype(int); y.index = X.index

# 3. Walk-forward CV with embargo/purge ------------------------------------
splitter = WalkForwardSplitter(
    WalkForwardConfig(train_window=200_000, test_window=50_000, embargo=15, purge=15)
)

def make_clf() -> DirectionClassifier:
    return DirectionClassifier(DirectionClassifierConfig(n_estimators=200, n_jobs=4))

metrics = walk_forward_evaluate(X, y, splitter, make_clf)
print(metrics[["fold", "accuracy", "auc", "sharpe", "trade_count"]])

# 4. Train + persist a final model -----------------------------------------
model = DirectionClassifier(DirectionClassifierConfig()).fit(X, y)
model.save("artefacts/direction_eurusd.joblib")
```

A runnable Phase 6 walk-forward driver sits at
`docs/examples/phase6_walk_forward_eurusd.py`. The 8-yr Dukascopy
bid/ask cache lives at `/paperclip/tradedesk/marketdata`.

### Leakage gate (CI merge-blocker)

Look-ahead bugs are silent killers; Phase 6 guards against them with two
independent gates:

* `tradedesk.ml.cv` enforces an embargo + purge at every fold boundary.
  The default `embargo=horizon, purge=horizon` matches the forward-label
  horizon so test rows can never overlap with train labels.
* `tests/ml/test_cv.py` carries the **leakage canary** — a synthetic
  feature column whose value equals the next bar's return. With the
  embargo/purge in place the canary must produce <0.55 OOS accuracy /
  AUC; without it the canary scores ~1.0. CI promotes this canary to a
  separate top-level step:

  ```yaml
  - name: Phase 6 leakage gate (tradedesk/ml/)
    run: pytest -m leakage --no-cov
  ```

  Any code change that breaks the embargo/purge contract trips the gate
  and the build fails. Add new feature columns? Re-run `pytest -m
  leakage` locally and confirm the canary still scores at chance level.

### Walk-forward reporting & feature importance

`tradedesk.ml.reporting` renders a markdown report (per-fold metrics,
feature-importance gain, leakage sanity panel, equity curve) from a
`walk_forward_collect` run. See `scripts/render_sample_report.py` for a
reproducible synthetic example.

### Streaming integration

`tradedesk.strategy.MLDirectionStrategy` plugs a trained
`predict_proba` model into the live event loop: rolling 1-min history
buffer → `FeatureBuilder.transform` → probability → `Signal` →
`SignalGeneratedEvent`. The `[ig_trader]` repository hosts a worked
example wired into the existing portfolio CLI.


## Documentation

See the `docs/` directory for:

-   Backtesting guide
-   Strategy guide
-   Portfolio guide
-   Indicator guide
-   Aggregation guide
-   Risk management guide
-   Metrics guide
-   Settings and operational tunables
-   Operational resilience and monitoring
-   ML label engineering (`docs/ml_labels_guide.md`)

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
