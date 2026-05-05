"""Unit tests for :mod:`tradedesk.ml.cv`.

Two families of tests:

* **Splitter contract** — sample-by-sample assertions on fold geometry,
  including the embargo/purge gap that constitutes the temporal-leakage
  guarantee.
* **Leakage gate** — end-to-end runs of :func:`walk_forward_evaluate` on
  synthetic data:
    - a feature that *literally* encodes the label drives accuracy to
      ~1.0 across folds (proves the harness measures leakage), and
    - a pure-noise feature lands at ~0.5 (negative control).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradedesk.ml.cv import (
    DEFAULT_PERIODS_PER_YEAR,
    FoldMetrics,
    WalkForwardConfig,
    WalkForwardSplitter,
    aggregate_fold_metrics,
    fold_metrics_from_predictions,
    walk_forward_evaluate,
)
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig


# ============================================================ splitter contract


def test_config_validates_window_and_gap_arguments() -> None:
    with pytest.raises(ValueError, match="train_window"):
        WalkForwardConfig(train_window=0, test_window=10)
    with pytest.raises(ValueError, match="test_window"):
        WalkForwardConfig(train_window=10, test_window=0)
    with pytest.raises(ValueError, match="step"):
        WalkForwardConfig(train_window=10, test_window=10, step=0)
    with pytest.raises(ValueError, match="embargo"):
        WalkForwardConfig(train_window=10, test_window=10, embargo=-1)
    with pytest.raises(ValueError, match="purge"):
        WalkForwardConfig(train_window=10, test_window=10, purge=-1)


def test_splitter_rejects_both_config_and_kwargs() -> None:
    with pytest.raises(TypeError, match="config="):
        WalkForwardSplitter(WalkForwardConfig(train_window=5, test_window=5), embargo=1)


def test_splitter_basic_geometry_no_gap() -> None:
    """Non-overlapping test windows, no embargo/purge, sliding train."""
    splitter = WalkForwardSplitter(train_window=20, test_window=10)
    folds = list(splitter.split(50))

    # n=50, train=20, test=10, step=10 → first test_start = 20, last = 40
    assert [f.test_start for f in folds] == [20, 30, 40]
    assert [f.test_end for f in folds] == [30, 40, 50]
    assert [f.train_start for f in folds] == [0, 10, 20]
    assert [f.train_end for f in folds] == [20, 30, 40]
    for f in folds:
        # No gap between train end and test start.
        assert f.gap == 0
        # Test fold is exactly test_window long.
        assert f.test_idx.tolist() == list(range(f.test_start, f.test_end))
        # Train fold is exactly train_window long when sliding.
        assert f.train_idx.tolist() == list(range(f.train_start, f.train_end))


def test_purge_drops_overlapping_train_rows() -> None:
    """Sample-by-sample assertion that purge removes the last `purge` train rows.

    With purge=h, the gap between train_end and test_start is exactly h —
    i.e. no train row at position ``test_start - 1 - i`` for ``i < h`` is
    present in the train fold. This is the temporal-leakage guarantee
    described by López de Prado for forward-horizon labels of horizon h.
    """
    splitter = WalkForwardSplitter(
        train_window=50, test_window=20, purge=5, embargo=0
    )
    folds = list(splitter.split(150))

    for f in folds:
        # Gap is exactly purge + embargo.
        assert f.gap == 5
        # The 5 positions immediately before test_start must not be in train.
        forbidden = set(range(f.test_start - 5, f.test_start))
        assert forbidden.isdisjoint(set(f.train_idx.tolist()))
        # And nothing further away than (test_start - gap - 1) is excluded
        # for sliding-window reasons alone — i.e. the row at test_start-6
        # IS in the train fold.
        assert f.test_start - 6 in set(f.train_idx.tolist())


def test_embargo_and_purge_are_additive() -> None:
    """The gap between train end and test start is exactly embargo + purge."""
    splitter = WalkForwardSplitter(
        train_window=80, test_window=20, embargo=3, purge=4
    )
    folds = list(splitter.split(200))
    for f in folds:
        assert f.gap == 7
        assert f.train_end == f.test_start - 7


def test_step_smaller_than_test_window_overlaps_test_folds() -> None:
    splitter = WalkForwardSplitter(train_window=20, test_window=10, step=5)
    folds = list(splitter.split(60))
    starts = [f.test_start for f in folds]
    # First start = 20 (train_window). Step = 5. Last start so test_end <= n=60 → 50.
    assert starts == [20, 25, 30, 35, 40, 45, 50]
    # Adjacent test folds overlap by 5.
    for prev, nxt in zip(folds, folds[1:]):
        assert prev.test_end - nxt.test_start == 5


def test_expanding_anchors_train_start_at_zero() -> None:
    splitter = WalkForwardSplitter(
        train_window=20, test_window=10, expanding=True
    )
    folds = list(splitter.split(60))
    # train_window is ignored when expanding; train_start always 0.
    assert all(f.train_start == 0 for f in folds)
    # train_end grows with each fold.
    assert [f.train_end for f in folds] == [20, 30, 40, 50]


def test_splitter_n_splits_matches_split_iteration() -> None:
    splitter = WalkForwardSplitter(
        train_window=30, test_window=15, embargo=2, purge=3
    )
    n = 180
    assert splitter.n_splits(n) == len(list(splitter.split(n)))


def test_splitter_raises_when_no_fold_fits() -> None:
    # n is too small: train_window=50 + test_window=50 + gap=10 = 110 > 80.
    splitter = WalkForwardSplitter(
        train_window=50, test_window=50, embargo=5, purge=5
    )
    with pytest.raises(ValueError, match="0 folds"):
        list(splitter.split(80))


def test_splitter_accepts_dataframe_and_datetime_index() -> None:
    idx = pd.date_range("2024-01-01", periods=80, freq="min")
    df = pd.DataFrame({"x": np.arange(80)}, index=idx)

    splitter = WalkForwardSplitter(train_window=40, test_window=10)
    by_df = list(splitter.split(df))
    by_idx = list(splitter.split(idx))
    by_int = list(splitter.split(80))

    assert [f.test_start for f in by_df] == [f.test_start for f in by_idx]
    assert [f.test_start for f in by_df] == [f.test_start for f in by_int]


def test_splitter_rejects_non_monotonic_datetime_index() -> None:
    bad = pd.DatetimeIndex(
        ["2024-01-02", "2024-01-01", "2024-01-03", "2024-01-04"] * 20
    )
    splitter = WalkForwardSplitter(train_window=20, test_window=10)
    with pytest.raises(ValueError, match="monotonic"):
        list(splitter.split(bad))


def test_repr_includes_window_and_gap_parameters() -> None:
    splitter = WalkForwardSplitter(
        train_window=50, test_window=20, step=5, embargo=2, purge=3
    )
    text = repr(splitter)
    for token in ("train_window=50", "test_window=20", "step=5", "embargo=2", "purge=3"):
        assert token in text


# ============================================================ metric core tests


def _flat_proba(n: int, p: float) -> np.ndarray:
    return np.full(n, p, dtype=float)


def test_fold_metrics_perfect_predictions() -> None:
    y = np.array([0, 1, 0, 1, 1, 0])
    proba = np.array([0.01, 0.99, 0.02, 0.98, 0.97, 0.05])
    m = fold_metrics_from_predictions(
        fold=0,
        n_train=100,
        train_start=0,
        train_end=100,
        test_start=100,
        test_end=106,
        y_true=y,
        p_up=proba,
    )
    assert m.accuracy == pytest.approx(1.0)
    assert m.auc == pytest.approx(1.0)
    assert m.hit_rate == pytest.approx(1.0)
    assert m.log_loss < 0.05


def test_fold_metrics_random_predictions_are_chance_level() -> None:
    rng = np.random.default_rng(0)
    n = 4000
    y = rng.integers(0, 2, n)
    proba = rng.uniform(0.0, 1.0, n)
    m = fold_metrics_from_predictions(
        fold=0,
        n_train=n,
        train_start=0,
        train_end=n,
        test_start=n,
        test_end=2 * n,
        y_true=y.astype(np.int64),
        p_up=proba,
    )
    assert 0.45 < m.accuracy < 0.55
    assert 0.45 < m.auc < 0.55


def test_fold_metrics_threshold_validation() -> None:
    with pytest.raises(ValueError, match="threshold"):
        fold_metrics_from_predictions(
            fold=0,
            n_train=10,
            train_start=0,
            train_end=10,
            test_start=10,
            test_end=20,
            y_true=np.zeros(10, dtype=np.int64),
            p_up=np.zeros(10),
            threshold=0.4,
        )


def test_fold_metrics_shape_validation() -> None:
    with pytest.raises(ValueError, match="shape"):
        fold_metrics_from_predictions(
            fold=0,
            n_train=10,
            train_start=0,
            train_end=10,
            test_start=10,
            test_end=20,
            y_true=np.zeros(10, dtype=np.int64),
            p_up=np.zeros(8),
        )


def test_fold_metrics_with_forward_returns_populates_sharpe_and_drawdown() -> None:
    rng = np.random.default_rng(7)
    n = 200
    # Strong "edge": positions are sign(p_up - 0.5) and forward_returns line up.
    p_up = rng.uniform(0.51, 0.95, n)
    forward = rng.normal(0.001, 0.005, n)
    y = (forward > 0).astype(np.int64)
    m = fold_metrics_from_predictions(
        fold=0,
        n_train=400,
        train_start=0,
        train_end=400,
        test_start=400,
        test_end=400 + n,
        y_true=y,
        p_up=p_up,
        forward_returns=forward,
        threshold=0.5,
    )
    assert m.trade_count == n
    assert np.isfinite(m.sharpe)
    assert np.isfinite(m.max_drawdown)
    assert m.max_drawdown <= 0.0


def test_fold_metrics_no_forward_returns_emits_nan_risk_metrics() -> None:
    n = 32
    rng = np.random.default_rng(0)
    y = rng.integers(0, 2, n).astype(np.int64)
    proba = rng.uniform(0, 1, n)
    m = fold_metrics_from_predictions(
        fold=0,
        n_train=64,
        train_start=0,
        train_end=64,
        test_start=64,
        test_end=64 + n,
        y_true=y,
        p_up=proba,
    )
    assert np.isnan(m.sharpe)
    assert np.isnan(m.max_drawdown)
    assert m.trade_count == 0


def test_fold_metrics_auc_returns_nan_for_single_class() -> None:
    y = np.zeros(10, dtype=np.int64)
    proba = _flat_proba(10, 0.3)
    m = fold_metrics_from_predictions(
        fold=0,
        n_train=10,
        train_start=0,
        train_end=10,
        test_start=10,
        test_end=20,
        y_true=y,
        p_up=proba,
    )
    assert np.isnan(m.auc)


def test_fold_metrics_hit_rate_nan_when_no_actionable_prediction() -> None:
    n = 20
    p_up = _flat_proba(n, 0.55)
    y = np.zeros(n, dtype=np.int64)
    m = fold_metrics_from_predictions(
        fold=0,
        n_train=10,
        train_start=0,
        train_end=10,
        test_start=10,
        test_end=10 + n,
        y_true=y,
        p_up=p_up,
        threshold=0.9,
    )
    assert np.isnan(m.hit_rate)


def test_aggregate_fold_metrics_returns_indexed_dataframe() -> None:
    rows = [
        FoldMetrics(
            fold=i,
            n_train=100,
            n_test=10,
            train_start=0,
            train_end=100,
            test_start=100,
            test_end=110,
            log_loss=0.5,
            accuracy=0.5 + 0.01 * i,
            auc=0.5,
            hit_rate=0.5,
            sharpe=float("nan"),
            max_drawdown=float("nan"),
            trade_count=0,
        )
        for i in range(3)
    ]
    df = aggregate_fold_metrics(rows)
    assert df.index.name == "fold"
    assert list(df.index) == [0, 1, 2]
    assert "accuracy" in df.columns
    assert df.loc[2, "accuracy"] == pytest.approx(0.52)


def test_aggregate_empty_returns_empty_frame() -> None:
    df = aggregate_fold_metrics([])
    assert df.empty


# ============================================================ leakage gate suite


def _fast_classifier_factory() -> DirectionClassifier:
    return DirectionClassifier(
        DirectionClassifierConfig(
            n_estimators=40,
            max_depth=3,
            learning_rate=0.2,
            early_stopping_rounds=None,
            seed=11,
            n_jobs=1,
        )
    )


def _make_random_walk(n: int = 1500, *, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """Random-walk close prices with a 5-bar forward-return binary label.

    Returns ``(X_template, y)`` where ``X_template`` only carries the index
    so callers can attach feature columns without re-deriving the labels.
    """
    rng = np.random.default_rng(seed)
    h = 5
    increments = rng.normal(0.0, 1.0, n + h)
    close = 100.0 + increments.cumsum()
    forward_return = close[h : n + h] - close[:n]
    y = pd.Series((forward_return > 0).astype(np.int64), name="y")
    X_template = pd.DataFrame(index=pd.RangeIndex(n))
    return X_template, y


def test_leakage_gate_obvious_feature_leak_drives_accuracy_to_one() -> None:
    """If we feed the model a feature that *is* the label, the harness must
    measure near-perfect accuracy across every fold.

    This proves XGBoost + the harness will faithfully report leakage when
    the user accidentally pipes future information into a feature column.
    Without this sensitivity the harness wouldn't be a leakage gate at all.
    """
    X_template, y = _make_random_walk()
    rng = np.random.default_rng(123)
    # Leaky feature: the label itself, with a sliver of noise so the boost
    # has a reason to make multiple splits and not collapse to a constant.
    leak = y.to_numpy().astype(float) + rng.normal(0.0, 0.05, len(y))
    X = X_template.assign(leak=leak)

    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    metrics = walk_forward_evaluate(
        X, y, splitter, _fast_classifier_factory
    )

    assert not metrics.empty
    assert metrics["accuracy"].min() >= 0.95, metrics
    assert metrics["auc"].min() >= 0.95, metrics


def test_leakage_gate_legitimate_feature_lands_at_chance_accuracy() -> None:
    """With a pure-noise feature, every fold must collapse to ~50% accuracy.

    Negative control: proves the splitter / metrics aggregator do not
    invent a spurious edge when none exists.
    """
    X_template, y = _make_random_walk(n=2400, seed=1)
    rng = np.random.default_rng(7)
    noise = rng.normal(0.0, 1.0, len(y))
    X = X_template.assign(noise=noise)

    splitter = WalkForwardSplitter(train_window=600, test_window=200, purge=5)
    metrics = walk_forward_evaluate(
        X, y, splitter, _fast_classifier_factory
    )

    assert not metrics.empty
    mean_accuracy = float(metrics["accuracy"].mean())
    # Generous chance band to keep the test stable across XGBoost versions.
    assert 0.42 < mean_accuracy < 0.58, metrics


# ============================================================ driver-level shape


def test_walk_forward_evaluate_aligns_input_shapes() -> None:
    X_template, y = _make_random_walk(n=300)
    X = X_template.assign(x=np.zeros(len(y)))
    bad_y = y.iloc[: len(y) - 1]
    with pytest.raises(ValueError, match="length mismatch"):
        walk_forward_evaluate(
            X,
            bad_y,
            WalkForwardSplitter(train_window=100, test_window=50),
            _fast_classifier_factory,
        )


def test_walk_forward_evaluate_requires_index_alignment() -> None:
    X_template, y = _make_random_walk(n=300)
    X = X_template.assign(x=np.zeros(len(y)))
    misaligned = y.copy()
    misaligned.index = pd.RangeIndex(start=1, stop=len(y) + 1)
    with pytest.raises(ValueError, match="index"):
        walk_forward_evaluate(
            X,
            misaligned,
            WalkForwardSplitter(train_window=100, test_window=50),
            _fast_classifier_factory,
        )


def test_walk_forward_evaluate_drops_nan_labels_per_fold() -> None:
    """NaN-labelled rows (warmup / horizon trailing) must be dropped per fold
    *after* the split so purge / embargo are still applied positionally."""
    X_template, y_full = _make_random_walk(n=600)
    rng = np.random.default_rng(2)
    y_with_nan = y_full.astype("Int64")
    # Force the trailing 5 rows to NaN (mimics horizon=5 forward return labels).
    y_with_nan.iloc[-5:] = pd.NA
    X = X_template.assign(noise=rng.normal(size=len(y_with_nan)))

    splitter = WalkForwardSplitter(train_window=200, test_window=100, purge=5)
    # Use a float Series so we can pass NaN through pandas.notna() in cv.py.
    metrics = walk_forward_evaluate(
        X,
        y_with_nan.astype("Float64").astype(float),
        splitter,
        _fast_classifier_factory,
    )

    # The driver should still produce metrics for at least one fold, and the
    # last fold's n_test should reflect the dropped NaN rows.
    assert not metrics.empty
    last = metrics.iloc[-1]
    assert last["n_test"] <= 100


def test_splitter_rejects_bool_sample_count() -> None:
    splitter = WalkForwardSplitter(train_window=5, test_window=5)
    with pytest.raises(TypeError, match="bool"):
        list(splitter.split(True))


def test_walk_forward_evaluate_rejects_misaligned_forward_returns() -> None:
    X_template, y = _make_random_walk(n=300)
    X = X_template.assign(x=np.zeros(len(y)))
    splitter = WalkForwardSplitter(train_window=100, test_window=50)

    short = pd.Series(np.zeros(len(y) - 1), index=y.index[:-1])
    with pytest.raises(ValueError, match="forward_returns"):
        walk_forward_evaluate(X, y, splitter, _fast_classifier_factory, forward_returns=short)

    misindexed = pd.Series(np.zeros(len(y)), index=pd.RangeIndex(start=1, stop=len(y) + 1))
    with pytest.raises(ValueError, match="forward_returns"):
        walk_forward_evaluate(X, y, splitter, _fast_classifier_factory, forward_returns=misindexed)


def test_fold_metrics_rejects_misaligned_forward_returns() -> None:
    with pytest.raises(ValueError, match="forward_returns"):
        fold_metrics_from_predictions(
            fold=0,
            n_train=10,
            train_start=0,
            train_end=10,
            test_start=10,
            test_end=20,
            y_true=np.zeros(10, dtype=np.int64),
            p_up=np.full(10, 0.6),
            forward_returns=np.zeros(8),
        )


def test_walk_forward_evaluate_with_forward_returns_populates_risk_columns() -> None:
    X_template, y = _make_random_walk(n=900)
    rng = np.random.default_rng(4)
    X = X_template.assign(x=rng.normal(size=len(y)))
    fwd = pd.Series(rng.normal(0.0, 0.001, len(y)), index=y.index)

    splitter = WalkForwardSplitter(train_window=400, test_window=100)
    metrics = walk_forward_evaluate(
        X,
        y,
        splitter,
        _fast_classifier_factory,
        forward_returns=fwd,
        periods_per_year=DEFAULT_PERIODS_PER_YEAR,
    )

    assert not metrics.empty
    assert metrics["trade_count"].sum() > 0
    assert metrics["max_drawdown"].le(0.0).all()
