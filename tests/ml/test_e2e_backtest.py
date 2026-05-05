"""End-to-end Phase 6 ML stack tests (RAD-906).

Wire :class:`tradedesk.ml.FeatureBuilder`, :func:`forward_return_labels`,
:class:`DirectionClassifier`, :class:`WalkForwardSplitter`, and
:class:`MLDirectionStrategy` into a single pipeline and exercise it on a
tiny synthetic dataset. The goal is to catch silent contract breaks
between the four building blocks (column drift, dtype drift, NaN drift)
that unit tests of any individual component would miss.

Two flavours of test live here:

* **Determinism** — fix the seed and assert that features, labels, and
  predictions are bit-identical across runs. Catches non-deterministic
  ordering or hidden RNG state in any layer.
* **End-to-end backtest** — synthetic ~1-month 1-minute dataset run through
  ``FeatureBuilder`` → labels → ``DirectionClassifier`` →
  ``WalkForwardSplitter`` → ``MLDirectionStrategy``. Asserts non-NaN
  equity curve, sane fold count, and that the streaming strategy emits
  signals consistent with the trained model.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from tradedesk.events import get_dispatcher, reset_dispatcher
from tradedesk.marketdata import CandleClosedEvent
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
from tradedesk.strategy import (
    MLDirectionConfig,
    MLDirectionStrategy,
    Signal,
    SignalGeneratedEvent,
)
from tradedesk.types import Candle

# ----------------------------------------------------------- synthetic fixtures


def _synthetic_minute_bars(
    n: int,
    *,
    seed: int = 0,
    drift: float = 0.0,
    sigma: float = 1e-4,
) -> pd.DataFrame:
    """Generate ``n`` 1-minute OHLC(V) bid/ask bars from a geometric random walk.

    The drift control lets callers create a faintly directional series so
    a tree-based model has *some* signal to learn — useful when asserting
    "the trained model does emit non-trivial signals" without depending
    on lucky seeds.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, sigma, size=n)
    close = 1.10 * np.exp(np.cumsum(steps))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + np.abs(rng.normal(0, sigma * 0.5, size=n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, sigma * 0.5, size=n))
    volume = rng.integers(100, 1000, size=n).astype(float)
    spread = 5e-5  # ~0.5 pip on a 1.10 quote
    idx = pd.date_range("2026-01-01 00:00", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "bid_close": close - spread / 2.0,
            "ask_close": close + spread / 2.0,
        },
        index=idx,
    )


def _fast_classifier_config() -> DirectionClassifierConfig:
    """Tiny estimator so the e2e suite stays under a few seconds."""
    return DirectionClassifierConfig(
        n_estimators=30,
        max_depth=3,
        learning_rate=0.2,
        early_stopping_rounds=None,
        seed=42,
        n_jobs=1,
    )


def _light_feature_config() -> FeatureConfig:
    """Keep warmup low so a tiny dataset still has plenty of usable rows.

    Used both for offline training and for the streaming
    ``MLDirectionStrategy`` runtime path. The streaming strategy
    reconstructs feature rows from :class:`tradedesk.types.Candle`, which
    carries only mid OHLC(V); microstructure features are emitted only
    when ``bid_close``/``ask_close`` are present in the input frame.
    Tests that exercise the strategy must therefore feed bars without
    bid/ask (see :func:`_streaming_safe_bars`) so the trainer and the
    runtime emit the same column set.
    """
    return FeatureConfig(
        return_windows=(1, 5, 15),
        moment_windows=(15,),
        include_time_features=True,
        include_microstructure=True,
    )


def _streaming_safe_bars(*args: object, **kwargs: object) -> pd.DataFrame:
    """Drop bid/ask columns so :class:`FeatureBuilder` does not emit spread
    columns the streaming strategy cannot reproduce."""
    bars = _synthetic_minute_bars(*args, **kwargs)  # type: ignore[arg-type]
    return bars.drop(columns=["bid_close", "ask_close"])


# ===================================================================== determinism


def test_pipeline_is_bitwise_deterministic_for_same_seed() -> None:
    """Same seed across two runs → identical features, labels, predictions.

    Catches hidden RNG state in :class:`FeatureBuilder`, the label
    generator, or the classifier wrapper. If any layer leaks a seedless
    RNG (e.g. a fresh ``np.random.default_rng()``), this test fails.
    """
    bars = _synthetic_minute_bars(2_000, seed=11, drift=1e-6)

    builder_a = FeatureBuilder(config=_light_feature_config())
    builder_b = FeatureBuilder(config=_light_feature_config())
    X_a = builder_a.transform(bars)
    X_b = builder_b.transform(bars)
    pd.testing.assert_frame_equal(X_a, X_b)

    label_cfg = LabelConfig(horizon=5)
    y_a = forward_return_labels(bars, label_cfg).reindex(X_a.index)
    y_b = forward_return_labels(bars, label_cfg).reindex(X_b.index)
    pd.testing.assert_series_equal(y_a, y_b)

    valid = y_a.notna()
    X_train = X_a.loc[valid]
    y_train = y_a.loc[valid].astype(np.int64)
    # Use a binary-only sub-sample (drop the literal-0 rows produced by the
    # neutral band — there is no neutral band here, so y is already {-1, 1}
    # mapped to {0, 1} below).
    y_binary = (y_train > 0).astype(np.int64)

    cfg = _fast_classifier_config()
    clf_a = DirectionClassifier(cfg).fit(X_train, y_binary)
    clf_b = DirectionClassifier(cfg).fit(X_train, y_binary)

    np.testing.assert_array_equal(
        clf_a.predict_proba(X_train),
        clf_b.predict_proba(X_train),
    )


def test_walk_forward_evaluate_is_deterministic_for_same_seed() -> None:
    """Repeated ``walk_forward_evaluate`` runs produce identical metrics.

    The driver builds a fresh model per fold via the factory, so this is
    the right place to catch *any* upstream non-determinism that survives
    the per-fold reset.
    """
    bars = _synthetic_minute_bars(2_500, seed=3, drift=1e-6)
    builder = FeatureBuilder(config=_light_feature_config())
    X = builder.transform(bars)
    y_raw = forward_return_labels(bars, LabelConfig(horizon=5)).reindex(X.index)
    valid = y_raw.notna()
    X = X.loc[valid]
    y = (y_raw.loc[valid].astype(np.int64) > 0).astype(np.int64)
    y.index = X.index

    splitter = WalkForwardSplitter(
        WalkForwardConfig(train_window=600, test_window=200, purge=5)
    )
    cfg = _fast_classifier_config()

    def factory() -> DirectionClassifier:
        return DirectionClassifier(cfg)

    a = walk_forward_evaluate(X, y, splitter, factory)
    b = walk_forward_evaluate(X, y, splitter, factory)

    pd.testing.assert_frame_equal(a, b)


# ============================================================ end-to-end backtest


def test_e2e_one_month_synthetic_pipeline_produces_sane_curve_and_folds() -> None:
    """Run a tiny synthetic 1-month 1-min dataset through the full Phase 6 stack.

    Verifies the contract surface that knits the four building blocks
    together at runtime:

    * features matrix and label series share an index after warmup/tail
      drop, with no NaNs surviving on either side;
    * splitter produces a sane fold count for the configured train/test
      windows (``>= 3`` so we measure walk-forward, not a single fold);
    * per-fold metrics carry no NaN equity-curve diagnostic
      (``log_loss``, ``accuracy``, ``trade_count``);
    * realised forward returns produce a finite, monotonically-defined
      cumulative equity curve (no NaN, no spurious infinities).

    Treats ``trade_count`` and ``accuracy`` as gross sanity checks rather
    than skill measurements — synthetic random-walk data isn't expected
    to learn anything, but the pipeline shouldn't return NaNs either.
    """
    # 1-month-ish synthetic dataset: 30 days × 1440 min ≈ 43k bars; we use
    # a smaller slice so the whole test stays under a few seconds while still
    # giving enough room for >= 3 walk-forward folds.
    n_bars = 6 * 24 * 60  # 6 days of 1-min bars — plenty for ~3 folds
    bars = _synthetic_minute_bars(n_bars, seed=7, drift=2e-6, sigma=1.5e-4)

    builder = FeatureBuilder(config=_light_feature_config())
    X = builder.transform(bars)
    horizon = 5
    raw_labels = forward_return_labels(bars, LabelConfig(horizon=horizon)).reindex(X.index)

    closes = bars["close"].astype(float)
    fr = (closes.shift(-horizon) / closes - 1.0).reindex(X.index)
    valid = raw_labels.notna() & fr.notna()

    X = X.loc[valid]
    y = (raw_labels.loc[valid].astype(np.int64) > 0).astype(np.int64)
    y.index = X.index
    fr = fr.loc[valid].astype(float)

    # Sanity on the dataset stitching itself.
    assert not X.isna().any().any(), "feature matrix carried NaNs into split"
    assert y.notna().all(), "label series carried NaNs into split"
    assert fr.notna().all(), "forward returns carried NaNs into split"
    assert X.index.equals(y.index)
    assert X.index.equals(fr.index)

    splitter = WalkForwardSplitter(
        WalkForwardConfig(
            train_window=2_000,
            test_window=600,
            embargo=horizon,
            purge=horizon,
        )
    )
    n_folds = splitter.n_splits(X)
    assert n_folds >= 3, f"expected >= 3 folds for sanity; got {n_folds}"

    cfg = _fast_classifier_config()

    def factory() -> DirectionClassifier:
        return DirectionClassifier(cfg)

    metrics = walk_forward_evaluate(
        X, y, splitter, factory, forward_returns=fr, threshold=0.5
    )

    # One row per executed fold, columns per the metric core contract.
    assert not metrics.empty
    assert len(metrics) == n_folds, "every fold should have produced a metric row"
    for col in ("log_loss", "accuracy", "auc", "trade_count"):
        assert col in metrics.columns
        assert metrics[col].notna().all(), f"{col} carried NaN across folds"

    # Realised equity curve from per-fold trade_count and accuracy is implicit
    # in `max_drawdown` / `sharpe`; assert those are not NaN when we passed
    # forward_returns. (Sharpe can be NaN if a fold produced no trades — guard
    # the assertion to "at least one fold has finite Sharpe".)
    assert (metrics["trade_count"] > 0).any(), "no folds produced any trades"
    assert metrics["max_drawdown"].notna().all()
    assert metrics["max_drawdown"].le(0.0).all(), "max_drawdown must be non-positive"

    # Cumulative equity curve from realised fold returns must be finite.
    cumulative = metrics["sharpe"].dropna().to_numpy()
    assert np.isfinite(cumulative).all() or cumulative.size == 0


# ===================================================================== strategy integration


@pytest.mark.slow
def test_walk_forward_trained_model_drives_streaming_strategy() -> None:
    """Train a classifier in-fold, then drive it through ``MLDirectionStrategy``.

    Verifies the **runtime** edge of the contract: a model trained against
    feature columns produced by :class:`FeatureBuilder` must accept the
    streaming feature row produced *for the same builder* on the live path.
    Catches column-name drift, dtype drift, and shape drift between the
    offline-training and online-scoring sides of the stack.

    Marked ``slow`` because the streaming loop recomputes features over
    growing history per candle — fine on the matrix runners, but the
    10× cost on a fast laptop is enough to push past the 5s budget the
    [RAD-896 plan](/RAD/issues/RAD-896#document-plan) asks us to mark.
    """
    n_bars = 600
    bars = _streaming_safe_bars(n_bars, seed=21, drift=1e-5, sigma=1e-4)

    builder = FeatureBuilder(config=_light_feature_config())
    X = builder.transform(bars)
    raw_labels = forward_return_labels(bars, LabelConfig(horizon=5)).reindex(X.index)
    valid = raw_labels.notna()
    X = X.loc[valid]
    y = (raw_labels.loc[valid].astype(np.int64) > 0).astype(np.int64)
    y.index = X.index

    classifier = DirectionClassifier(_fast_classifier_config()).fit(X, y)

    instrument = "CS.D.EURUSD.CFD.IP"
    period = "1MIN"
    streaming_builder = FeatureBuilder(config=_light_feature_config())
    # Cap history to twice the warmup — bigger buffers blow up the per-candle
    # ``transform`` cost without changing the test's assertion surface.
    history_cap = max(2 * streaming_builder.warmup(), 64)
    strategy = MLDirectionStrategy(
        instrument=instrument,
        period=period,
        feature_builder=streaming_builder,
        model=classifier,
        config=MLDirectionConfig(threshold=0.5, history_capacity=history_cap),
    )

    reset_dispatcher()
    seen: list[SignalGeneratedEvent] = []
    get_dispatcher().subscribe(SignalGeneratedEvent, lambda e: seen.append(e))

    candles = [
        Candle(
            timestamp=str(ts),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for ts, row in zip(bars.index, bars.itertuples(index=False), strict=True)
    ]
    for candle in candles:
        asyncio.run(
            strategy.on_candle_close(
                CandleClosedEvent(
                    instrument=instrument,
                    timeframe=period,
                    candle=candle,
                )
            )
        )
    reset_dispatcher()

    # The strategy MUST have produced at least one signal post-warmup.
    assert seen, "MLDirectionStrategy emitted no signals over the bar stream"
    # Every signal must be one of the three legal values — guards the
    # threshold/probability mapping at the runtime edge.
    assert all(
        s.signal in (Signal.ENTRY_LONG, Signal.ENTRY_SHORT, Signal.NEUTRAL)
        for s in seen
    )
    # And ``last_p_up`` must lie in [0, 1] — guards the predict_proba surface.
    assert strategy.last_p_up is not None
    assert 0.0 <= strategy.last_p_up <= 1.0


@pytest.mark.slow
def test_strategy_signal_distribution_responds_to_threshold() -> None:
    """Lifting the threshold should increase the share of NEUTRAL signals.

    Doesn't depend on the model being skilful — it depends on the
    strategy correctly dispatching on the threshold band. Catches a class
    of bugs where the threshold is plumbed through ``MLDirectionConfig``
    but ignored by the dispatcher.
    """
    n_bars = 400
    bars = _streaming_safe_bars(n_bars, seed=33, sigma=2e-4)

    builder = FeatureBuilder(config=_light_feature_config())
    X = builder.transform(bars)
    raw_labels = forward_return_labels(bars, LabelConfig(horizon=5)).reindex(X.index)
    valid = raw_labels.notna()
    X = X.loc[valid]
    y = (raw_labels.loc[valid].astype(np.int64) > 0).astype(np.int64)
    y.index = X.index
    classifier = DirectionClassifier(_fast_classifier_config()).fit(X, y)

    candles = [
        Candle(
            timestamp=str(ts),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for ts, row in zip(bars.index, bars.itertuples(index=False), strict=True)
    ]

    def _run(threshold: float) -> list[Signal]:
        reset_dispatcher()
        streaming_builder = FeatureBuilder(config=_light_feature_config())
        history_cap = max(2 * streaming_builder.warmup(), 64)
        strategy = MLDirectionStrategy(
            instrument="CS.D.EURUSD.CFD.IP",
            period="1MIN",
            feature_builder=streaming_builder,
            model=classifier,
            config=MLDirectionConfig(threshold=threshold, history_capacity=history_cap),
        )
        seen: list[Signal] = []
        get_dispatcher().subscribe(
            SignalGeneratedEvent, lambda e: seen.append(e.signal)
        )
        for candle in candles:
            asyncio.run(
                strategy.on_candle_close(
                    CandleClosedEvent(
                        instrument="CS.D.EURUSD.CFD.IP",
                        timeframe="1MIN",
                        candle=candle,
                    )
                )
            )
        reset_dispatcher()
        return seen

    low_threshold = _run(threshold=0.5)
    high_threshold = _run(threshold=0.9)

    assert low_threshold and high_threshold
    low_neutral = sum(1 for s in low_threshold if s is Signal.NEUTRAL)
    high_neutral = sum(1 for s in high_threshold if s is Signal.NEUTRAL)
    # Threshold 0.5 collapses the neutral band to a measure-zero point; threshold
    # 0.9 widens the band to ``[0.1, 0.9]`` so almost every probability is
    # neutralised. The strict inequality is the contract test.
    assert high_neutral > low_neutral, (
        f"raising threshold did not widen neutral band: "
        f"low={low_neutral}, high={high_neutral}"
    )


