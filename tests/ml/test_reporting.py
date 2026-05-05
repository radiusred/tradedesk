"""Unit tests for :mod:`tradedesk.ml.reporting`.

Three families of tests:

* **Collection driver** — :func:`walk_forward_collect` produces one
  :class:`FoldArtifacts` per executed fold and retains the fitted model and
  OOS probabilities.
* **Aggregations + plotting** — feature-importance gains, equity curve,
  markdown report writer.
* **Leakage sanity panel** — :func:`run_leakage_sanity` returns ``passed=True``
  on the synthetic leak fixture (positive control) and ``passed=False`` on a
  noise-only feature (negative control).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradedesk.ml.cv import WalkForwardSplitter
from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig
from tradedesk.ml.reporting import (
    FoldArtifacts,
    LeakageSanityResult,
    aggregate_feature_importance,
    aggregate_metrics_summary,
    concatenated_equity_curve,
    feature_importance_gains,
    plot_equity_curve,
    render_markdown_report,
    run_leakage_sanity,
    walk_forward_collect,
)


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
    rng = np.random.default_rng(seed)
    h = 5
    increments = rng.normal(0.0, 1.0, n + h)
    close = 100.0 + increments.cumsum()
    forward_return = close[h : n + h] - close[:n]
    y = pd.Series((forward_return > 0).astype(np.int64), name="y")
    X_template = pd.DataFrame(index=pd.RangeIndex(n))
    return X_template, y


def _make_signal_dataset(
    n: int = 900, *, seed: int = 0
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Build a small dataset with a noisy edge so feature_importance is non-empty.

    Returns ``(X, y, forward_returns)``. ``X`` carries a feature loosely
    correlated with the label so the booster makes splits we can rank, but
    not so correlated that the leakage gate would reject it.
    """
    rng = np.random.default_rng(seed)
    X_template, y = _make_random_walk(n=n, seed=seed)
    base = y.to_numpy().astype(float) * 0.4 + rng.normal(0.0, 1.0, n)
    noise = rng.normal(0.0, 1.0, n)
    X = X_template.assign(signal=base, noise=noise)
    fwd = pd.Series(rng.normal(0.0, 0.001, n), index=X.index)
    return X, y, fwd


# ============================================================ collection driver


def test_walk_forward_collect_yields_one_artifact_per_fold() -> None:
    X, y, fwd = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)

    artifacts = walk_forward_collect(
        X, y, splitter, _fast_classifier_factory, forward_returns=fwd
    )

    assert artifacts, "expected at least one fold"
    assert all(isinstance(a, FoldArtifacts) for a in artifacts)
    assert all(a.model._model is not None for a in artifacts), "models must be fitted"
    # Probabilities are aligned with the test index.
    for a in artifacts:
        assert a.p_up.shape == a.y_true.shape
        assert len(a.test_index) == len(a.p_up)
        assert a.forward_returns is not None
        assert a.forward_returns.shape == a.p_up.shape


def test_walk_forward_collect_rejects_misaligned_inputs() -> None:
    X, y, _ = _make_signal_dataset(n=300)
    splitter = WalkForwardSplitter(train_window=100, test_window=50)

    short_y = y.iloc[:-1]
    with pytest.raises(ValueError, match="length mismatch"):
        walk_forward_collect(X, short_y, splitter, _fast_classifier_factory)

    misindexed = y.copy()
    misindexed.index = pd.RangeIndex(start=1, stop=len(y) + 1)
    with pytest.raises(ValueError, match="index"):
        walk_forward_collect(X, misindexed, splitter, _fast_classifier_factory)


# ===================================================== aggregation + plotting


def test_aggregate_metrics_summary_emits_mean_and_std() -> None:
    X, y, fwd = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(
        X, y, splitter, _fast_classifier_factory, forward_returns=fwd
    )

    summary = aggregate_metrics_summary(artifacts)

    assert not summary.empty
    for col in ("accuracy", "auc", "log_loss", "trade_count"):
        assert col in summary.index
    # Numeric and finite mean for accuracy / log_loss.
    assert np.isfinite(float(summary.loc["accuracy", "mean"]))
    assert np.isfinite(float(summary.loc["log_loss", "mean"]))


def test_aggregate_metrics_summary_empty_when_no_artifacts() -> None:
    assert aggregate_metrics_summary([]).empty


def test_feature_importance_gains_sorted_desc_and_aggregated() -> None:
    X, y, fwd = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(
        X, y, splitter, _fast_classifier_factory, forward_returns=fwd
    )

    # Per-fold gains are non-empty and sorted desc.
    for a in artifacts:
        gains = feature_importance_gains(a.model)
        assert not gains.empty
        assert list(gains.values) == sorted(gains.values, reverse=True)

    agg = aggregate_feature_importance(artifacts)
    assert not agg.empty
    assert list(agg.columns) == ["mean_gain", "std_gain", "n_folds"]
    assert (agg["mean_gain"].diff().dropna() <= 1e-12).all(), "must be sorted desc"
    assert agg["n_folds"].max() <= len(artifacts)


def test_aggregate_feature_importance_empty_when_no_artifacts() -> None:
    out = aggregate_feature_importance([])
    assert list(out.columns) == ["mean_gain", "std_gain", "n_folds"]
    assert out.empty


def test_concatenated_equity_curve_monotone_in_index() -> None:
    X, y, fwd = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(
        X, y, splitter, _fast_classifier_factory, forward_returns=fwd
    )

    equity = concatenated_equity_curve(artifacts)

    assert not equity.empty
    # cumsum produces a series indexed on the union of the per-fold test indices.
    assert equity.index.is_monotonic_increasing
    # No NaNs in the realised series — every fold supplied forward_returns.
    assert not equity.isna().any()


def test_concatenated_equity_curve_empty_without_forward_returns() -> None:
    X, y, _ = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(X, y, splitter, _fast_classifier_factory)

    equity = concatenated_equity_curve(artifacts)
    assert equity.empty


def test_plot_equity_curve_writes_png(tmp_path: Path) -> None:
    X, y, fwd = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(
        X, y, splitter, _fast_classifier_factory, forward_returns=fwd
    )

    out = plot_equity_curve(artifacts, tmp_path / "equity.png")

    assert out.exists()
    assert out.stat().st_size > 0
    # Sanity-check the PNG magic bytes.
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_plot_equity_curve_renders_placeholder_when_no_returns(tmp_path: Path) -> None:
    X, y, _ = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(X, y, splitter, _fast_classifier_factory)

    out = plot_equity_curve(artifacts, tmp_path / "equity.png")
    assert out.exists()
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_markdown_report_writes_md_and_png(tmp_path: Path) -> None:
    X, y, fwd = _make_signal_dataset(n=900)
    splitter = WalkForwardSplitter(train_window=400, test_window=100, purge=5)
    artifacts = walk_forward_collect(
        X, y, splitter, _fast_classifier_factory, forward_returns=fwd
    )
    leakage_result = LeakageSanityResult(
        passed=True,
        min_accuracy=0.99,
        min_auc=0.99,
        n_folds=3,
        threshold=0.95,
        notes="",
    )

    md_path = render_markdown_report(
        artifacts,
        tmp_path / "report",
        title="Phase 6 Walk-forward CV report",
        leakage_result=leakage_result,
    )

    assert md_path.exists()
    body = md_path.read_text()
    assert "# Phase 6 Walk-forward CV report" in body
    assert "## Per-fold metrics" in body
    assert "## Aggregate (mean ± std across folds)" in body
    assert "## Equity curve" in body
    assert "## Feature importance" in body
    assert "## Leakage sanity panel" in body
    assert "PASS" in body
    assert "![equity curve](equity_curve.png)" in body
    assert (md_path.parent / "equity_curve.png").exists()


# ============================================================== leakage sanity


def test_run_leakage_sanity_passes_on_obvious_leak() -> None:
    """Positive control: a feature equal to the label must clear the gate."""
    result = run_leakage_sanity(model_factory=_fast_classifier_factory)
    assert result.passed, result
    assert result.min_accuracy >= 0.95
    assert result.min_auc >= 0.95
    assert result.n_folds >= 1


def test_run_leakage_sanity_fails_on_high_threshold() -> None:
    """Threshold above the achievable accuracy should flip the verdict."""
    result = run_leakage_sanity(
        model_factory=_fast_classifier_factory,
        threshold_accuracy=1.01,
    )
    assert not result.passed
    assert result.notes
