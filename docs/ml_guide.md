# Machine learning guide (`tradedesk.ml`)

This guide is a compact reference for the optional machine-learning surface in
`tradedesk`.

## Installation

ML support is optional:

```bash
pip install 'tradedesk[ml]'
```

The `[ml]` extra installs the dependencies used by model and reporting
components, including `xgboost`, `scikit-learn`, and `joblib`.

## What `tradedesk.ml` includes

The public ML package is organized around four building blocks:

- `FeatureBuilder` and `FeatureConfig` for feature engineering over time-indexed
  OHLC(V) data, with optional bid/ask-aware features
- `forward_return_labels(...)` and `triple_barrier_labels(...)` for supervised
  label generation
- `WalkForwardSplitter` and `walk_forward_evaluate(...)` for leakage-aware
  walk-forward evaluation
- `DirectionClassifier` and `MLDirectionStrategy` for model training and event
  loop integration

Import these surfaces from:

```python
from tradedesk.ml import (
    FeatureBuilder,
    FeatureConfig,
    LabelConfig,
    WalkForwardConfig,
    WalkForwardSplitter,
    forward_return_labels,
    walk_forward_evaluate,
)
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig
from tradedesk.strategy import MLDirectionStrategy
```

## Feature engineering

`FeatureBuilder` turns a time-indexed `pandas.DataFrame` of bars into a feature
matrix suitable for training or inference.

Built-in features include:

- Lagged log returns over multiple horizons
- Rolling realised volatility and higher moments
- Time-of-day and weekday encodings
- Outputs from `tradedesk.marketdata.indicators`
- Optional microstructure features derived from candle shape and bid/ask spread

Example:

```python
from tradedesk.ml import FeatureBuilder, FeatureConfig

builder = FeatureBuilder(config=FeatureConfig())
X = builder.transform(bars)
```

`bars` must use a monotonic `DatetimeIndex` and include the columns required by
the configured feature set.

## Labels

`tradedesk.ml.labels` supports:

- Forward-return labels via `forward_return_labels(...)`
- Triple-barrier labels via `triple_barrier_labels(...)`
- Class-balance summaries for fold diagnostics

Label-specific usage and field semantics are documented in
[ml_labels_guide.md](ml_labels_guide.md).

## Walk-forward evaluation

`WalkForwardSplitter` produces ordered train/test folds for time-series model
evaluation. The splitter supports purge and embargo settings to reduce leakage
at fold boundaries.

Example:

```python
from tradedesk.ml import WalkForwardConfig, WalkForwardSplitter

splitter = WalkForwardSplitter(
    WalkForwardConfig(train_window=200_000, test_window=50_000, embargo=15, purge=15)
)
```

Use `walk_forward_evaluate(...)` when you want a metrics table across those
folds.

## End-to-end example

```python
from tradedesk.ml import (
    FeatureBuilder,
    FeatureConfig,
    LabelConfig,
    WalkForwardConfig,
    WalkForwardSplitter,
    forward_return_labels,
    walk_forward_evaluate,
)
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig

builder = FeatureBuilder(config=FeatureConfig())
X = builder.transform(bars)

y_raw = forward_return_labels(bars, LabelConfig(horizon=15)).reindex(X.index)
valid = y_raw.notna()
X = X.loc[valid]
y = (y_raw.loc[valid] > 0).astype(int)
y.index = X.index

splitter = WalkForwardSplitter(
    WalkForwardConfig(train_window=200_000, test_window=50_000, embargo=15, purge=15)
)

def make_model() -> DirectionClassifier:
    return DirectionClassifier(DirectionClassifierConfig(n_estimators=200, n_jobs=4))

metrics = walk_forward_evaluate(X, y, splitter, make_model)
print(metrics[["fold", "accuracy", "auc", "sharpe", "trade_count"]])
```

## Strategy integration

`MLDirectionStrategy` is the runtime bridge between a trained probability model
and the normal tradedesk event loop. It maintains a rolling history buffer,
builds features from incoming candles, converts model probabilities into
signals, and emits the same strategy events used elsewhere in the framework.

Use it when you want ML inference to live inside a standard `BaseStrategy`
workflow rather than in a separate orchestration layer.

## Related docs

- [strategy_guide.md](strategy_guide.md)
- [backtesting_guide.md](backtesting_guide.md)
- [ml_labels_guide.md](ml_labels_guide.md)
- [examples/](examples/)
