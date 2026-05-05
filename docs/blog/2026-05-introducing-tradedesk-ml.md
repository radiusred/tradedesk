# Introducing `tradedesk.ml`: A No-Nonsense ML Pipeline for Python Traders

If you've tried to apply machine learning to tick or minute-bar data, you've probably encountered the same two landmines: **look-ahead bias** (your backtest beats the market because it cheated) and **train/live drift** (your features worked in the notebook, then behaved differently when wired to a live feed). Both are quiet killers — they don't throw exceptions, they just inflate your backtest Sharpe and leave you confused when live performance diverges.

`tradedesk.ml` is the ML layer inside [tradedesk](https://github.com/radiusred/tradedesk), a Python event-driven trading framework. It is opinionated, production-minded, and built around a single promise: if a feature was computed a certain way in training, it is computed *identically* at runtime. This post walks through what it does and how to use it.

---

## Installation

```bash
pip install 'tradedesk[ml]'
```

Base tradedesk (pandas, numpy) is always installed. The `[ml]` extra adds XGBoost, scikit-learn, joblib, and matplotlib. Notably, `from tradedesk.ml import FeatureBuilder` works without the extras — only the model and reporting surfaces require them.

---

## The Pipeline at a Glance

```
raw OHLCV bars
    → FeatureBuilder  (~50 features across six families, streaming indicators)
    → forward_return_labels / triple_barrier_labels
    → WalkForwardSplitter  (embargo + purge)
    → DirectionClassifier  (XGBoost, deterministic)
    → walk_forward_evaluate / render_markdown_report
    → MLDirectionStrategy  (live signal emission, same FeatureBuilder)
```

Each stage is independently composable. You can swap in your own model, use only the labelling utilities, or plug the walk-forward splitter into a scikit-learn pipeline.

---

## Feature Engineering: ~50 Features Across Six Families, One Invariant

`FeatureBuilder` takes a DataFrame of 1-minute OHLCV bars and returns an ML-ready feature matrix. It emits between 44 and 52 columns depending on configuration (regime/calendar toggles and whether `bid_close`/`ask_close` are supplied), grouped into six families:

**Lagged log returns** — momentum over 1, 5, 15, 60, and 240 bars (`log_ret_1`, `log_ret_60`, etc.)

**Rolling moment statistics** — volatility, skewness, kurtosis at multiple horizons (`vol_60`, `skew_240`, `kurt_15`)

**Technical indicator stack** — ADX, ATR, Bollinger Bands, CCI, EMA, Keltner Channels, MACD, MFI, OBV, RSI, SMA, Stochastic, VWAP, Williams %R

**Time-of-day features** — sine/cosine encoding of minute-of-day plus weekday integer, capturing FX session structure without a sawtooth discontinuity at midnight

**Microstructure features** — candle body/range/wick ratios; bid/ask spread and relative spread when bid/ask data is available

**Regime features** (optional) — percentile rank of yesterday's realized vol in a trailing 60-day window, plus rolling vol-of-vol; both use previous-day data and are forward-filled so there is no same-day leak

```python
from tradedesk.ml import FeatureBuilder, FeatureConfig
from datetime import date

config = FeatureConfig(
    include_regime_features=True,
    include_calendar_features=True,
    macro_event_dates=(date(2024, 3, 7), date(2024, 6, 6))  # FOMC dates
)
builder = FeatureBuilder(config=config)
X = builder.transform(bars)  # warmup rows dropped by default
```

**The critical detail:** all 14 technical indicators are *streamed* — fed bars in chronological order, exactly as they would be in a live strategy. The offline `transform()` call and the live per-bar update use the same code path. There is no shortcut where the offline path computes a rolling window with a look-back into the future while the live path doesn't. What you trained on is what you trade on.

---

## Label Engineering

Two label families cover most supervised setups.

### Forward-return labels (fast, simple)

```python
from tradedesk.ml import LabelConfig, forward_return_labels

y = forward_return_labels(bars, LabelConfig(
    horizon=5,           # look 5 bars ahead
    neutral_band=0.0001, # 1 bp threshold; below this → label 0
    spread_aware=True    # long return = bid[t+h] / ask[t] − 1
))
# Returns Int8 Series in {−1, 0, 1}; trailing 5 rows are NaN
```

`spread_aware=True` bakes in the round-trip cost before assigning a direction label, so you don't accidentally train on opportunities that get eaten by spread.

### Triple-barrier labels (López de Prado)

```python
from tradedesk.ml import TripleBarrierConfig, triple_barrier_labels

out = triple_barrier_labels(bars, TripleBarrierConfig(
    horizon=30,
    atr_period=14,
    barrier_mult=2.0,
    upper_mult=2.5,   # asymmetric: wider target than stop
    lower_mult=1.5,
))
# out.columns: label, exit_offset, barrier
```

Each bar gets a label based on which of three barriers is hit first within the horizon window: the upper target (+1), the lower stop (−1), or the time/vertical barrier (sign of close return). The `exit_offset` and `barrier` columns are useful for downstream position-sizing and for diagnosing which exits are dominating fold performance.

---

## Walk-Forward CV with Temporal Leakage Protection

This is where most ML-in-finance frameworks cut corners. The `WalkForwardSplitter` enforces two forms of temporal gap between train and test:

- **Purge** — drops the last `h` training rows, whose label windows overlap the test set
- **Embargo** — an additional gap beyond purge to absorb feature serial autocorrelation

Both are additive. The total buffer is `embargo + purge` samples.

```python
from tradedesk.ml import WalkForwardConfig, WalkForwardSplitter, walk_forward_evaluate

config = WalkForwardConfig(
    train_window=5000,
    test_window=500,
    step=500,
    embargo=30,
    purge=60,
    expanding=False  # sliding window; set True for anchored/expanding
)

metrics_df = walk_forward_evaluate(
    X=X,
    y=y,
    splitter=WalkForwardSplitter(config),
    model_factory=lambda: DirectionClassifier(),
    forward_returns=forward_returns_series,
    threshold=0.55
)
```

`walk_forward_evaluate` returns a DataFrame with per-fold metrics: log loss, accuracy, AUC, hit rate (accuracy on confident predictions above the threshold), annualized Sharpe, max drawdown, and trade count.

The framework ships a **leakage sanity check** — `run_leakage_sanity()` deliberately injects a future-leaking feature and verifies that accuracy collapses to near-1.0 on the training set and near-random on the test set. If the harness somehow failed to respect temporal ordering, the sanity check would catch it.

---

## The Model: `DirectionClassifier`

A thin, opinionated XGBoost wrapper with defaults tuned for the noise characteristics of 1-minute FX data:

```python
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig

model = DirectionClassifier(DirectionClassifierConfig(
    max_depth=4,           # shallow trees for noisy data
    reg_lambda=1.0,        # L2 regularization
    learning_rate=0.05,
    n_estimators=500,
    early_stopping_rounds=50,
    n_jobs=1               # single-threaded for bit-exact reproducibility
))

model.fit(X_train, y_train, eval_set=[(X_val, y_val)])
proba = model.predict_proba(X_test)  # (n, 2); column 1 = P(up)

model.save("eurusd_direction.pkl")
model_loaded = DirectionClassifier.load("eurusd_direction.pkl")
```

`n_jobs=1` is deliberate — it preserves bit-exact training across runs, which matters for regression testing and model versioning. If you're doing hyperparameter sweeps and want speed, flip it; for production model sign-off, keep it at 1.

---

## Overfitting Controls

Four layers sit on top of the base model:

1. **Validation tails** — a configurable slice of each train fold is held out for XGBoost early stopping, with an optional internal purge between the head and tail
2. **Grid sweep** (`walk_forward_sweep`) — small hyperparameter grid evaluated *inside* each fold; the validation tail picks the winner so the test set is never touched during tuning
3. **Feature importance pruning** (`feature_importance_gain_pruning`) — fits a base model, drops low-gain features by XGBoost gain, and compares full vs. pruned OOS metrics
4. **Probability calibration** (`PlattCalibrator`, `IsotonicCalibrator`) — per-fold calibration reporting Brier score before and after

---

## Reporting

`render_markdown_report()` generates a per-fold metrics table, aggregate mean ± std, and feature importance ranked by XGBoost gain across folds. `plot_equity_curve()` produces a matplotlib PNG of the concatenated OOS equity curve. Both are available in `tradedesk.ml.reporting` (lazily loaded — importing the feature/label modules stays cheap if you don't need matplotlib).

---

## Deploying Live: `MLDirectionStrategy`

The live strategy reuses the *exact same* `FeatureBuilder` instance from offline training:

```python
from tradedesk.strategy.ml_direction_strategy import MLDirectionStrategy, MLDirectionConfig

strategy = MLDirectionStrategy(
    instrument="EURUSD",
    period="1m",
    feature_builder=builder,   # same object used in training
    model=model,
    config=MLDirectionConfig(threshold=0.55, history_capacity=1024)
)
```

On each `CandleClosedEvent`, the strategy appends the new bar, recomputes features on a rolling buffer, calls `model.predict_proba()`, and emits `ENTRY_LONG` (p ≥ 0.55), `ENTRY_SHORT` (p ≤ 0.45), or `NEUTRAL`. Because the indicator stack is streamed rather than recomputed with a sliding window, live features are numerically identical to backtest features for the same sequence of bars.

---

## Where It Lives

The `tradedesk.ml` module ships with tradedesk proper. Source is in `tradedesk/ml/` — features, labels, cv, model, tuning, reporting, and walk_forward_runner are each in their own module with standalone unit tests in `tests/ml/`. An end-to-end sample walk-forward report (with per-fold metrics table, equity curve, and feature importance) lives in `docs/ml/sample-walk-forward-report/`.

The framework targets 1-minute FX bars and loads Dukascopy bid/ask cache data out of the box via `load_dukascopy_bidask_minutes()`. It is not limited to FX — any instrument with clean 1-minute OHLCV data works, though the indicator defaults and Sharpe annualization constant (252 × 24 × 60 = 362,880 minutes/year) assume 24-hour markets.

---

## What It Does Not Do

A few things worth naming explicitly: there is no automatic hyperparameter optimization (Optuna, Ray Tune, etc.) — the grid sweep is intentionally small to keep in-fold tuning tractable. There is no multi-asset or portfolio-level modelling. There is no deep learning integration. Those are on the roadmap; for now, the project's philosophy is to do one thing well: give you a technically honest baseline for supervised direction classification on minute bars.

---

If you're working on systematic trading in Python and want to build on top of it, the source is public at `github.com/radiusred/tradedesk`. Issues and PRs are open. The team is small, mostly agent-staffed, and moves fast.
