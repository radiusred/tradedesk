"""Unit tests for :mod:`tradedesk.ml.tuning` (Phase 6 / RAD-904).

Five focus areas:

* **Validation tail** — :class:`ValidationTailSpec` carving and
  :func:`make_validation_tail` head/tail alignment.
* **Param grid** — :class:`ParamGrid` cartesian product, validation, and
  override emission.
* **Sweep driver** — :func:`walk_forward_sweep` produces the expected
  ``n_folds * n_grid_points`` rows, never tunes on test, and threads the
  early-stopping eval set through XGBoost.
* **Pruning** — :func:`feature_importance_gain_pruning` drops the weakest
  features per fold and reports both variants.
* **Calibration** — Platt + isotonic calibrators, Brier score, reliability
  bins, and the per-fold calibration sweep.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradedesk.ml.cv import WalkForwardSplitter
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig
from tradedesk.ml.tuning import (
    BRIER_RELIABILITY_DEFAULT_BINS,
    IsotonicCalibrator,
    ParamGrid,
    PlattCalibrator,
    ValidationTailSpec,
    brier_score,
    feature_importance_gain_pruning,
    fit_calibrator,
    make_validation_tail,
    reliability_bins,
    walk_forward_calibration,
    walk_forward_sweep,
)

# ============================================================ validation tail


def test_validation_tail_spec_validates_arguments() -> None:
    with pytest.raises(ValueError, match="fraction"):
        ValidationTailSpec(fraction=0.0)
    with pytest.raises(ValueError, match="fraction"):
        ValidationTailSpec(fraction=1.0)
    with pytest.raises(ValueError, match="purge"):
        ValidationTailSpec(fraction=0.2, purge=-1)
    with pytest.raises(ValueError, match="min_tail"):
        ValidationTailSpec(fraction=0.2, min_tail=0)
    with pytest.raises(ValueError, match="min_head"):
        ValidationTailSpec(fraction=0.2, min_head=0)


def test_make_validation_tail_carves_last_fraction() -> None:
    n = 100
    X = pd.DataFrame({"x": np.arange(n, dtype=float)})
    y = pd.Series(np.arange(n) % 2, name="y")
    res = make_validation_tail(X, y, ValidationTailSpec(fraction=0.2))
    assert res is not None
    X_head, y_head, X_tail, y_tail = res
    assert len(X_head) == 80
    assert len(X_tail) == 20
    # Tail is the last 20 rows, head is the first 80 rows (no internal purge).
    assert X_head["x"].tolist() == list(range(80))
    assert X_tail["x"].tolist() == list(range(80, 100))
    assert y_head.tolist() == [i % 2 for i in range(80)]
    assert y_tail.tolist() == [i % 2 for i in range(80, 100)]


def test_make_validation_tail_applies_internal_purge() -> None:
    n = 100
    X = pd.DataFrame({"x": np.arange(n, dtype=float)})
    y = pd.Series(np.arange(n) % 2, name="y")
    res = make_validation_tail(X, y, ValidationTailSpec(fraction=0.2, purge=5))
    assert res is not None
    X_head, _, X_tail, _ = res
    # Tail still last 20; head loses 5 trailing rows to the internal purge.
    assert len(X_tail) == 20
    assert len(X_head) == 75
    assert X_head["x"].iloc[-1] == 74.0
    assert X_tail["x"].iloc[0] == 80.0


def test_make_validation_tail_returns_none_when_too_short() -> None:
    n = 4
    X = pd.DataFrame({"x": np.arange(n, dtype=float)})
    y = pd.Series([0, 1, 0, 1])
    # 4 * 0.2 -> 1 (rounded) tail; min_tail=2 should reject.
    assert make_validation_tail(X, y, ValidationTailSpec(fraction=0.2, min_tail=2)) is None
    # purge=4 leaves head_end = 3 - 4 = -1 < min_head=1
    assert make_validation_tail(X, y, ValidationTailSpec(fraction=0.2, purge=4)) is None


def test_make_validation_tail_rejects_length_mismatch() -> None:
    X = pd.DataFrame({"x": [0.0, 1.0, 2.0]})
    y = pd.Series([0, 1])
    with pytest.raises(ValueError, match="length mismatch"):
        make_validation_tail(X, y, ValidationTailSpec())


# ============================================================ param grid


def test_param_grid_default_cardinality() -> None:
    grid = ParamGrid()
    # Defaults: 3 * 2 * 2 * 2 = 24
    assert len(grid) == 24
    overrides = list(grid.iter_overrides())
    assert len(overrides) == 24
    # Each override carries the four regularisation knobs.
    for o in overrides:
        assert set(o.keys()) == {"max_depth", "min_child_weight", "gamma", "reg_lambda"}


def test_param_grid_custom_values_are_emitted_verbatim() -> None:
    grid = ParamGrid(
        max_depth=(2,),
        min_child_weight=(1.0, 2.0),
        gamma=(0.1,),
        reg_lambda=(0.5,),
    )
    overrides = list(grid.iter_overrides())
    assert len(overrides) == 2
    weights = sorted(o["min_child_weight"] for o in overrides)
    assert weights == [1.0, 2.0]
    for o in overrides:
        assert o["max_depth"] == 2
        assert o["gamma"] == pytest.approx(0.1)
        assert o["reg_lambda"] == pytest.approx(0.5)


def test_param_grid_validates_arguments() -> None:
    with pytest.raises(ValueError, match="max_depth"):
        ParamGrid(max_depth=())
    with pytest.raises(ValueError, match="max_depth"):
        ParamGrid(max_depth=(0,))
    with pytest.raises(ValueError, match="min_child_weight"):
        ParamGrid(min_child_weight=(-1.0,))
    with pytest.raises(ValueError, match="gamma"):
        ParamGrid(gamma=(-0.1,))
    with pytest.raises(ValueError, match="reg_lambda"):
        ParamGrid(reg_lambda=(-0.1,))


# ============================================================ calibration core


def test_brier_score_perfect_predictions_is_zero() -> None:
    y = np.array([0, 1, 1, 0], dtype=int)
    p = y.astype(float)
    assert brier_score(y, p) == pytest.approx(0.0)


def test_brier_score_constant_half_matches_class_variance() -> None:
    y = np.array([0, 1, 1, 0], dtype=int)
    p = np.full(4, 0.5)
    assert brier_score(y, p) == pytest.approx(0.25)


def test_brier_score_empty_returns_nan() -> None:
    assert np.isnan(brier_score(np.array([], dtype=int), np.array([], dtype=float)))


def test_brier_score_shape_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="shape"):
        brier_score(np.zeros(3, dtype=int), np.zeros(4, dtype=float))


def test_reliability_bins_default_shape_is_fixed() -> None:
    rng = np.random.default_rng(0)
    n = 1000
    y = rng.integers(0, 2, n)
    p = rng.uniform(0.0, 1.0, n)
    df = reliability_bins(y, p)
    assert len(df) == BRIER_RELIABILITY_DEFAULT_BINS
    # Bin edges cover [0, 1].
    assert df["bin_low"].iloc[0] == pytest.approx(0.0)
    assert df["bin_high"].iloc[-1] == pytest.approx(1.0)
    assert df["count"].sum() == n


def test_reliability_bins_empty_bins_keep_nan_means() -> None:
    # All probabilities in the same bin — every other bin must be empty.
    y = np.array([0, 1, 0, 1])
    p = np.array([0.55, 0.55, 0.55, 0.55])
    df = reliability_bins(y, p, n_bins=10)
    # Bin 5 spans [0.5, 0.6) so all 4 rows land there.
    assert int(df.loc[5, "count"]) == 4
    empty = df[df["count"] == 0]
    assert empty["mean_p"].isna().all()
    assert empty["mean_y"].isna().all()


def test_reliability_bins_validates_n_bins() -> None:
    y = np.array([0, 1])
    p = np.array([0.4, 0.6])
    with pytest.raises(ValueError, match="n_bins"):
        reliability_bins(y, p, n_bins=1)


def test_platt_calibrator_recovers_simple_logit_shift() -> None:
    """Pre-shifted probabilities should be pulled back toward truth by Platt."""
    rng = np.random.default_rng(0)
    n = 5000
    y = rng.integers(0, 2, n)
    # Uncalibrated: shift logit by +1 so the model is over-confident on class 1.
    base_p = 0.5 + 0.4 * (y - 0.5) + rng.normal(0.0, 0.05, n)
    base_p = np.clip(base_p, 0.05, 0.95)
    cal = PlattCalibrator().fit(base_p, y)
    out = cal.transform(base_p)
    # Calibrated Brier should be at least as good as the uncalibrated one.
    assert brier_score(y, out) <= brier_score(y, base_p) + 1e-6


def test_platt_calibrator_handles_single_class_calibration_set() -> None:
    p = np.array([0.2, 0.3, 0.4])
    y = np.array([1, 1, 1])
    cal = PlattCalibrator().fit(p, y)
    # Falls back to identity sigmoid(logit(p)) ≈ p.
    out = cal.transform(p)
    np.testing.assert_allclose(out, p, atol=1e-6)


def test_isotonic_calibrator_is_monotone() -> None:
    rng = np.random.default_rng(1)
    n = 500
    p = rng.uniform(0.0, 1.0, n)
    y = (p + rng.normal(0.0, 0.1, n) > 0.5).astype(int)
    cal = IsotonicCalibrator().fit(p, y)
    # Sample on a sorted grid; output should be non-decreasing.
    grid = np.linspace(0.0, 1.0, 50)
    out = cal.transform(grid)
    assert np.all(np.diff(out) >= -1e-9)


def test_isotonic_calibrator_handles_single_class() -> None:
    p = np.array([0.2, 0.3, 0.4])
    y = np.array([0, 0, 0])
    cal = IsotonicCalibrator().fit(p, y)
    out = cal.transform(p)
    # Single-class calibration set collapses to a constant equal to mean(y) = 0.
    np.testing.assert_allclose(out, np.zeros_like(p), atol=1e-9)


def test_fit_calibrator_dispatches_methods() -> None:
    p = np.array([0.2, 0.3, 0.7, 0.8])
    y = np.array([0, 0, 1, 1])
    assert isinstance(fit_calibrator("platt", p, y), PlattCalibrator)
    assert isinstance(fit_calibrator("isotonic", p, y), IsotonicCalibrator)
    with pytest.raises(ValueError, match="unknown calibration"):
        fit_calibrator("nope", p, y)  # type: ignore[arg-type]


# ============================================================ synthetic dataset


def _make_separable_dataset(
    n: int = 1500,
    *,
    seed: int = 0,
    h: int = 5,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Synthetic dataset where one feature has signal and the rest are noise.

    Used to verify the sweep / pruning / calibration drivers run end-to-end
    and produce non-degenerate metrics. Returns ``(X, y, forward_returns)``
    with NaN-trimmed labels.
    """
    rng = np.random.default_rng(seed)
    increments = rng.normal(0.0, 1.0, n + h)
    close = 100.0 + increments.cumsum()
    forward_return = pd.Series(close[h : n + h] - close[:n])
    y = (forward_return > 0).astype(int)
    signal = forward_return.shift(-1).fillna(0.0).to_numpy() + rng.normal(0.0, 0.5, n)
    X = pd.DataFrame(
        {
            "signal": signal,
            "noise_a": rng.normal(0.0, 1.0, n),
            "noise_b": rng.normal(0.0, 1.0, n),
            "noise_c": rng.normal(0.0, 1.0, n),
        }
    )
    return X, y, forward_return


def _fast_base_config() -> DirectionClassifierConfig:
    return DirectionClassifierConfig(
        n_estimators=30,
        max_depth=3,
        learning_rate=0.2,
        early_stopping_rounds=5,
        seed=11,
        n_jobs=1,
    )


# ============================================================ sweep driver


def test_walk_forward_sweep_emits_one_row_per_fold_and_grid_point() -> None:
    X, y, forward = _make_separable_dataset()
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    grid = ParamGrid(
        max_depth=(3,),
        min_child_weight=(1.0, 5.0),
        gamma=(0.0,),
        reg_lambda=(1.0,),
    )
    df = walk_forward_sweep(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        grid=grid,
        validation_tail=ValidationTailSpec(fraction=0.2),
        forward_returns=forward,
    )
    n_folds = splitter.n_splits(len(X))
    assert len(df) == n_folds * len(grid)
    # Hyperparameter columns are present on every row.
    for col in ("max_depth", "min_child_weight", "gamma", "reg_lambda"):
        assert col in df.columns
    # Sharpe / drawdown populated thanks to forward_returns.
    assert df["trade_count"].max() > 0
    # Feature-set tag defaults to "all".
    assert (df["feature_set"] == "all").all()
    assert (df["n_features"] == X.shape[1]).all()


def test_walk_forward_sweep_passes_eval_set_to_classifier(monkeypatch) -> None:
    """Validation tail must reach DirectionClassifier.fit as eval_set."""
    X, y, _ = _make_separable_dataset(n=600)
    splitter = WalkForwardSplitter(train_window=200, test_window=100, purge=2)
    captured: list[bool] = []

    real_fit = DirectionClassifier.fit

    def spy_fit(self, X_in, y_in, *, eval_set=None):
        captured.append(eval_set is not None)
        return real_fit(self, X_in, y_in, eval_set=eval_set)

    monkeypatch.setattr(DirectionClassifier, "fit", spy_fit)

    walk_forward_sweep(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        grid=ParamGrid(max_depth=(3,), min_child_weight=(1.0,), gamma=(0.0,), reg_lambda=(1.0,)),
        validation_tail=ValidationTailSpec(fraction=0.2),
    )
    assert captured  # at least one fit happened
    assert all(captured), "every sweep fit should receive eval_set"


def test_walk_forward_sweep_without_validation_tail_fits_full_train(monkeypatch) -> None:
    X, y, _ = _make_separable_dataset(n=600)
    splitter = WalkForwardSplitter(train_window=200, test_window=100, purge=2)
    captured: list[bool] = []

    real_fit = DirectionClassifier.fit

    def spy_fit(self, X_in, y_in, *, eval_set=None):
        captured.append(eval_set is not None)
        return real_fit(self, X_in, y_in, eval_set=eval_set)

    monkeypatch.setattr(DirectionClassifier, "fit", spy_fit)

    walk_forward_sweep(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        grid=ParamGrid(max_depth=(3,), min_child_weight=(1.0,), gamma=(0.0,), reg_lambda=(1.0,)),
        validation_tail=None,
    )
    assert captured
    assert not any(captured), "without a validation tail, eval_set must be None"


def test_walk_forward_sweep_feature_set_restricts_columns() -> None:
    X, y, _ = _make_separable_dataset(n=600)
    splitter = WalkForwardSplitter(train_window=200, test_window=100, purge=2)
    df = walk_forward_sweep(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        grid=ParamGrid(max_depth=(3,), min_child_weight=(1.0,), gamma=(0.0,), reg_lambda=(1.0,)),
        feature_set=["signal", "noise_a"],
        feature_set_name="signal_only",
    )
    assert (df["feature_set"] == "signal_only").all()
    assert (df["n_features"] == 2).all()


def test_walk_forward_sweep_rejects_unknown_columns() -> None:
    X, y, _ = _make_separable_dataset(n=300)
    splitter = WalkForwardSplitter(train_window=100, test_window=50, purge=2)
    with pytest.raises(ValueError, match="not in X"):
        walk_forward_sweep(
            X,
            y,
            splitter,
            feature_set=["signal", "missing_feature"],
        )


def test_walk_forward_sweep_aligns_input_shapes() -> None:
    X, y, _ = _make_separable_dataset(n=300)
    splitter = WalkForwardSplitter(train_window=100, test_window=50, purge=2)
    bad_y = y.iloc[:-1]
    with pytest.raises(ValueError, match="length mismatch"):
        walk_forward_sweep(X, bad_y, splitter)


# ============================================================ feature pruning


def test_feature_importance_gain_pruning_drops_weakest_features() -> None:
    X, y, _ = _make_separable_dataset(n=900, seed=2)
    splitter = WalkForwardSplitter(train_window=300, test_window=150, purge=5)
    df = feature_importance_gain_pruning(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        drop_quantile=0.5,
    )
    assert not df.empty
    # Each fold contributes a "full" and "pruned" row.
    counts = df.groupby("fold")["variant"].nunique()
    assert (counts == 2).all()
    # Pruned variant keeps strictly fewer features in at least one fold.
    pruned = df[df["variant"] == "pruned"]
    full = df[df["variant"] == "full"]
    assert (pruned["n_features_kept"].to_numpy() <= full["n_features_kept"].to_numpy()).all()
    assert pruned["n_features_kept"].min() < full["n_features_kept"].min()


def test_feature_importance_gain_pruning_validates_quantile() -> None:
    X, y, _ = _make_separable_dataset(n=300)
    splitter = WalkForwardSplitter(train_window=100, test_window=50, purge=2)
    with pytest.raises(ValueError, match="drop_quantile"):
        feature_importance_gain_pruning(X, y, splitter, drop_quantile=0.0)
    with pytest.raises(ValueError, match="drop_quantile"):
        feature_importance_gain_pruning(X, y, splitter, drop_quantile=1.0)


# ============================================================ calibration sweep


def test_walk_forward_calibration_reports_brier_before_and_after() -> None:
    X, y, _ = _make_separable_dataset(n=900, seed=3)
    splitter = WalkForwardSplitter(train_window=300, test_window=150, purge=5)
    summary, per_fold = walk_forward_calibration(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        method="platt",
        calibration_tail=ValidationTailSpec(fraction=0.2),
    )
    assert not summary.empty
    assert summary.index.name == "fold"
    for col in ("brier_uncalibrated", "brier_calibrated", "brier_delta", "method"):
        assert col in summary.columns
    assert (summary["method"] == "platt").all()
    # Reliability tables exist for every fold and have the default bin count.
    assert set(per_fold.keys()) == set(summary.index.tolist())
    for fold_id, rel in per_fold.items():
        assert len(rel) == BRIER_RELIABILITY_DEFAULT_BINS
        assert rel["count"].sum() > 0


def test_walk_forward_calibration_isotonic_runs_end_to_end() -> None:
    X, y, _ = _make_separable_dataset(n=900, seed=4)
    splitter = WalkForwardSplitter(train_window=300, test_window=150, purge=5)
    summary, _ = walk_forward_calibration(
        X,
        y,
        splitter,
        base_config=_fast_base_config(),
        method="isotonic",
    )
    assert not summary.empty
    assert (summary["method"] == "isotonic").all()
    # Calibrated probabilities stay finite, so Brier scores must be finite too.
    assert summary["brier_uncalibrated"].notna().all()
    assert summary["brier_calibrated"].notna().all()
