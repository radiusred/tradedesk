"""Overfitting controls for the Phase 6 walk-forward sweep.

The four controls layered on top of :class:`tradedesk.ml.model.DirectionClassifier`
plus :class:`tradedesk.ml.cv.WalkForwardSplitter`:

* **Early stopping** — :class:`ValidationTailSpec` carves the *tail* of each
  train fold into a validation slice (with optional internal purge) so XGBoost
  can stop on a plateau in validation log-loss without ever seeing test data.
* **Regularisation sweep** — :class:`ParamGrid` enumerates a small grid over
  ``max_depth``, ``min_child_weight``, ``gamma``, ``reg_lambda`` and
  :func:`walk_forward_sweep` runs each grid point through the supplied
  :class:`WalkForwardSplitter` so tuning happens *only* inside walk-forward
  folds. The validation tail (if configured) is the only signal used to pick a
  per-fold winner — we never touch the test fold for selection.
* **Feature-importance pruning** —
  :func:`feature_importance_gain_pruning` fits the base config on each train
  fold, drops the bottom-quantile features by XGBoost gain, refits, and emits
  a tidy DataFrame comparing full vs pruned OOS metrics fold-by-fold.
* **Probability calibration** — :class:`PlattCalibrator` and
  :class:`IsotonicCalibrator` fit a per-fold calibrator on a tail of the train
  fold; :func:`walk_forward_calibration` reports per-fold Brier score before
  and after calibration plus reliability-bin counts.

All evaluation flows go through :func:`tradedesk.ml.cv.walk_forward_evaluate`
or its sibling sweep driver below — we never optimise hyperparameters on
in-sample data once the CV harness is locked (enforced by the
``no_in_sample_tuning`` flag on :func:`walk_forward_sweep`).

Importing this module requires the ``[ml]`` extra
(``pip install 'tradedesk[ml]'``) — :mod:`tradedesk.ml.model` is a hard
dependency.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, Protocol

import numpy as np
import pandas as pd

from .cv import (
    DEFAULT_PERIODS_PER_YEAR,
    WalkForwardSplitter,
    fold_metrics_from_predictions,
)
from .model import DirectionClassifier, DirectionClassifierConfig

__all__ = [
    "BRIER_RELIABILITY_DEFAULT_BINS",
    "Calibrator",
    "IsotonicCalibrator",
    "ParamGrid",
    "PlattCalibrator",
    "ValidationTailSpec",
    "brier_score",
    "feature_importance_gain_pruning",
    "fit_calibrator",
    "make_validation_tail",
    "reliability_bins",
    "walk_forward_calibration",
    "walk_forward_sweep",
]


#: Default number of reliability bins emitted by :func:`reliability_bins`.
BRIER_RELIABILITY_DEFAULT_BINS: Final[int] = 10


# ============================================================ validation tails


@dataclass(frozen=True)
class ValidationTailSpec:
    """How to carve a validation tail out of a train fold.

    The tail is the last ``fraction`` of the (filtered) train fold, with an
    optional internal ``purge`` of samples between the head and tail to absorb
    serial-autocorrelation that bleeds across the boundary.

    Attributes:
        fraction: Fraction of the train fold to reserve for validation. Must
            satisfy ``0 < fraction < 1``.
        purge: Number of samples dropped from the *head* between the head end
            and the tail start. Same role as the outer fold purge, applied
            *within* the train fold. Must be ``>= 0``.
        min_tail: Minimum acceptable tail size, in samples. When the carving
            produces fewer rows the fold is treated as ineligible for early
            stopping / calibration.
        min_head: Minimum acceptable head size, in samples. Same eligibility
            semantics as ``min_tail``.
    """

    fraction: float = 0.2
    purge: int = 0
    min_tail: int = 1
    min_head: int = 1

    def __post_init__(self) -> None:
        if not 0.0 < self.fraction < 1.0:
            raise ValueError("fraction must satisfy 0 < fraction < 1")
        if self.purge < 0:
            raise ValueError("purge must be >= 0")
        if self.min_tail < 1:
            raise ValueError("min_tail must be >= 1")
        if self.min_head < 1:
            raise ValueError("min_head must be >= 1")


@dataclass(frozen=True)
class _TailIndices:
    head_idx: np.ndarray
    tail_idx: np.ndarray


def _carve_tail(n: int, spec: ValidationTailSpec) -> _TailIndices | None:
    """Carve positional head/tail indices out of a fold of length ``n``.

    Returns ``None`` when the carving would violate ``min_head`` / ``min_tail``.
    """
    tail_size = int(round(n * spec.fraction))
    if tail_size < spec.min_tail:
        return None
    tail_start = n - tail_size
    head_end = tail_start - spec.purge
    if head_end < spec.min_head:
        return None
    head_idx = np.arange(0, head_end, dtype=np.int64)
    tail_idx = np.arange(tail_start, n, dtype=np.int64)
    return _TailIndices(head_idx=head_idx, tail_idx=tail_idx)


def make_validation_tail(
    X_train: pd.DataFrame,
    y_train: pd.Series,
    spec: ValidationTailSpec,
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame, pd.Series] | None:
    """Carve ``(X_train, y_train)`` into ``(head, tail)`` using ``spec``.

    The split is positional — the same logic the rest of the harness uses —
    so the *last* ``fraction`` rows become the validation tail and the head
    keeps everything before the optional internal purge.

    Returns:
        ``(X_head, y_head, X_tail, y_tail)`` or ``None`` when the carving
        would violate the ``min_head`` / ``min_tail`` floors in ``spec``.
    """
    if len(X_train) != len(y_train):
        raise ValueError("X_train and y_train length mismatch")
    indices = _carve_tail(len(X_train), spec)
    if indices is None:
        return None
    X_head = X_train.iloc[indices.head_idx]
    y_head = y_train.iloc[indices.head_idx]
    X_tail = X_train.iloc[indices.tail_idx]
    y_tail = y_train.iloc[indices.tail_idx]
    return X_head, y_head, X_tail, y_tail


# ============================================================ regularisation grid


@dataclass(frozen=True)
class ParamGrid:
    """Grid over the four regularisation knobs Phase 6 sweeps over.

    The grid is intentionally small — Comp 6's budget is 2 days and the
    sweep runs *inside* every walk-forward fold, so the cartesian product is
    multiplied by ``n_folds`` already.

    Attributes:
        max_depth: Tree depth values to try.
        min_child_weight: Minimum hessian per leaf to try.
        gamma: Minimum loss reduction per split to try.
        reg_lambda: L2 regularisation on leaf weights to try.
    """

    max_depth: Sequence[int] = (3, 4, 5)
    min_child_weight: Sequence[float] = (1.0, 5.0)
    gamma: Sequence[float] = (0.0, 0.1)
    reg_lambda: Sequence[float] = (1.0, 5.0)

    def __post_init__(self) -> None:
        for name in ("max_depth", "min_child_weight", "gamma", "reg_lambda"):
            values = getattr(self, name)
            if len(values) == 0:
                raise ValueError(f"{name} grid must contain at least one value")
        for d in self.max_depth:
            if d < 1:
                raise ValueError("max_depth values must be >= 1")
        for w in self.min_child_weight:
            if w < 0:
                raise ValueError("min_child_weight values must be >= 0")
        for g in self.gamma:
            if g < 0:
                raise ValueError("gamma values must be >= 0")
        for lam in self.reg_lambda:
            if lam < 0:
                raise ValueError("reg_lambda values must be >= 0")

    def iter_overrides(self) -> Iterator[dict[str, Any]]:
        """Yield one ``dict`` of config overrides per grid point."""
        for max_depth, mcw, gamma, lam in itertools.product(
            self.max_depth, self.min_child_weight, self.gamma, self.reg_lambda
        ):
            yield {
                "max_depth": int(max_depth),
                "min_child_weight": float(mcw),
                "gamma": float(gamma),
                "reg_lambda": float(lam),
            }

    def __len__(self) -> int:
        return (
            len(self.max_depth)
            * len(self.min_child_weight)
            * len(self.gamma)
            * len(self.reg_lambda)
        )


# ============================================================ calibration


class Calibrator(Protocol):
    """Minimal callable interface implemented by :class:`PlattCalibrator` etc."""

    def fit(self, p_uncal: np.ndarray, y_true: np.ndarray) -> "Calibrator": ...
    def transform(self, p_uncal: np.ndarray) -> np.ndarray: ...


class PlattCalibrator:
    """Logistic (Platt) calibration in logit space.

    Maps an uncalibrated probability ``p`` to ``sigmoid(a * logit(p) + b)``
    where ``(a, b)`` are fit by maximum-likelihood logistic regression on the
    calibration tail. Equivalent to scikit-learn's ``CalibratedClassifierCV``
    with ``method='sigmoid'`` but without the dependency on a base estimator.
    """

    _LOGIT_EPS: Final[float] = 1e-6

    def __init__(self) -> None:
        self.a_: float | None = None
        self.b_: float | None = None

    @staticmethod
    def _logit(p: np.ndarray) -> np.ndarray:
        clipped = np.clip(p, PlattCalibrator._LOGIT_EPS, 1.0 - PlattCalibrator._LOGIT_EPS)
        return np.asarray(np.log(clipped / (1.0 - clipped)), dtype=float)

    @staticmethod
    def _sigmoid(z: np.ndarray) -> np.ndarray:
        return np.asarray(1.0 / (1.0 + np.exp(-z)), dtype=float)

    def fit(self, p_uncal: np.ndarray, y_true: np.ndarray) -> "PlattCalibrator":
        from sklearn.linear_model import LogisticRegression

        logit = self._logit(np.asarray(p_uncal, dtype=float)).reshape(-1, 1)
        y = np.asarray(y_true, dtype=int)
        if y.size == 0 or np.unique(y).size < 2:
            # Degenerate calibration set — fall back to identity.
            self.a_ = 1.0
            self.b_ = 0.0
            return self
        lr = LogisticRegression(C=1e6, solver="lbfgs")
        lr.fit(logit, y)
        self.a_ = float(lr.coef_[0, 0])
        self.b_ = float(lr.intercept_[0])
        return self

    def transform(self, p_uncal: np.ndarray) -> np.ndarray:
        if self.a_ is None or self.b_ is None:
            raise RuntimeError("PlattCalibrator is not fitted")
        z = self.a_ * self._logit(np.asarray(p_uncal, dtype=float)) + self.b_
        return self._sigmoid(z)


class IsotonicCalibrator:
    """Isotonic regression calibration.

    Wraps :class:`sklearn.isotonic.IsotonicRegression` with ``out_of_bounds``
    set to ``"clip"`` so calibration tails outside the training range map to
    the boundary values rather than NaN.
    """

    def __init__(self) -> None:
        self._iso: Any = None

    def fit(self, p_uncal: np.ndarray, y_true: np.ndarray) -> "IsotonicCalibrator":
        from sklearn.isotonic import IsotonicRegression

        p = np.asarray(p_uncal, dtype=float)
        y = np.asarray(y_true, dtype=int)
        iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
        if y.size == 0:
            self._iso = iso  # unfitted — transform will raise
            return self
        if np.unique(y).size < 2:
            # Degenerate calibration set — produce a constant mapping.
            iso.fit([0.0, 1.0], [float(y.mean()), float(y.mean())])
        else:
            iso.fit(p, y)
        self._iso = iso
        return self

    def transform(self, p_uncal: np.ndarray) -> np.ndarray:
        if self._iso is None:
            raise RuntimeError("IsotonicCalibrator is not fitted")
        return np.asarray(self._iso.transform(np.asarray(p_uncal, dtype=float)), dtype=float)


def fit_calibrator(
    method: Literal["platt", "isotonic"],
    p_uncal: np.ndarray,
    y_true: np.ndarray,
) -> Calibrator:
    """Fit and return a calibrator of the named flavour."""
    if method == "platt":
        return PlattCalibrator().fit(p_uncal, y_true)
    if method == "isotonic":
        return IsotonicCalibrator().fit(p_uncal, y_true)
    raise ValueError(f"unknown calibration method: {method!r}")


def brier_score(y_true: np.ndarray, p_up: np.ndarray) -> float:
    """Mean-squared-error between binary labels and predicted probabilities.

    Lower is better. NaN when ``y_true`` is empty.
    """
    if y_true.size == 0:
        return float("nan")
    if y_true.shape != p_up.shape:
        raise ValueError(f"y_true and p_up must share shape; got {y_true.shape} vs {p_up.shape}")
    diff = np.asarray(p_up, dtype=float) - np.asarray(y_true, dtype=float)
    return float(np.mean(diff * diff))


def reliability_bins(
    y_true: np.ndarray,
    p_up: np.ndarray,
    *,
    n_bins: int = BRIER_RELIABILITY_DEFAULT_BINS,
) -> pd.DataFrame:
    """Reliability-diagram bin counts.

    Bins ``[0, 1]`` into ``n_bins`` equal-width buckets and reports for each:

    * ``count`` — number of predictions falling into the bin
    * ``mean_p`` — mean predicted probability inside the bin
    * ``mean_y`` — mean realised label inside the bin (the empirical
      frequency a perfectly calibrated model would match)

    Empty bins keep ``count=0`` with NaN means so the table preserves a
    fixed-shape report regardless of the prediction distribution.
    """
    if n_bins < 2:
        raise ValueError("n_bins must be >= 2")
    if y_true.shape != p_up.shape:
        raise ValueError(f"y_true and p_up must share shape; got {y_true.shape} vs {p_up.shape}")

    edges = np.linspace(0.0, 1.0, n_bins + 1)
    # Clip to keep the rightmost bin closed.
    p = np.clip(np.asarray(p_up, dtype=float), 0.0, 1.0 - 1e-12)
    bin_idx = np.digitize(p, edges) - 1
    bin_idx = np.clip(bin_idx, 0, n_bins - 1)

    rows: list[dict[str, Any]] = []
    for b in range(n_bins):
        mask = bin_idx == b
        count = int(mask.sum())
        if count == 0:
            mean_p = float("nan")
            mean_y = float("nan")
        else:
            mean_p = float(np.mean(p[mask]))
            mean_y = float(np.mean(y_true[mask]))
        rows.append(
            {
                "bin": b,
                "bin_low": float(edges[b]),
                "bin_high": float(edges[b + 1]),
                "count": count,
                "mean_p": mean_p,
                "mean_y": mean_y,
            }
        )
    return pd.DataFrame(rows).set_index("bin")


# ============================================================ sweep driver


@dataclass(frozen=True)
class _SweepFoldRecord:
    """One row of the sweep results — fold geometry + params + metrics."""

    fold: int
    point_idx: int
    overrides: dict[str, Any]
    feature_set: str
    n_features: int
    metrics: dict[str, Any]


def _coerce_feature_set(
    feature_set: str | Sequence[str] | None,
    X: pd.DataFrame,
) -> tuple[str, list[str]]:
    """Resolve the optional feature-set tag/columns into ``(name, columns)``.

    ``feature_set`` may be:

    * ``None`` — use all columns of ``X`` and tag with ``"all"``.
    * A ``str`` — use all columns and tag with that name (e.g. ``"baseline"``).
    * A sequence of column names — restrict ``X`` to those columns and tag
      with ``"custom"`` (callers can pass an explicit name via the wrapping
      function).
    """
    if feature_set is None:
        return "all", list(X.columns)
    if isinstance(feature_set, str):
        return feature_set, list(X.columns)
    cols = list(feature_set)
    missing = [c for c in cols if c not in X.columns]
    if missing:
        raise ValueError(f"feature_set columns not in X: {missing}")
    return "custom", cols


def walk_forward_sweep(
    X: pd.DataFrame,
    y: pd.Series,
    splitter: WalkForwardSplitter,
    *,
    base_config: DirectionClassifierConfig | None = None,
    grid: ParamGrid | None = None,
    validation_tail: ValidationTailSpec | None = None,
    forward_returns: pd.Series | None = None,
    threshold: float = 0.5,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    feature_set: str | Sequence[str] | None = None,
    feature_set_name: str | None = None,
    no_in_sample_tuning: bool = True,
) -> pd.DataFrame:
    """Walk-forward sweep over a regularisation grid.

    For each ``(fold, grid_point)`` pair:

    1. Build a :class:`DirectionClassifierConfig` by overlaying the grid
       point on ``base_config``.
    2. If ``validation_tail`` is set, carve the train fold into
       ``(head, tail)`` and pass the tail as XGBoost's ``eval_set`` so
       early stopping can fire.
    3. Fit, score on the test fold, and emit a metrics row tagged with the
       feature set + hyperparameters.

    Args:
        X: Feature matrix aligned with ``y``.
        y: Binary-label series aligned with ``X``.
        splitter: Configured :class:`WalkForwardSplitter`.
        base_config: Starting config; the grid point overrides only the four
            regularisation knobs. Defaults to :class:`DirectionClassifierConfig`'s
            defaults.
        grid: Grid to sweep. Defaults to a small 3×2×2×2 grid that lines up
            with the Phase 6 plan budget.
        validation_tail: When set, controls the head/tail split used for
            early stopping.
        forward_returns: Forward-return series for Sharpe / drawdown columns.
        threshold: Probability threshold passed to the metric core.
        periods_per_year: Sharpe annualisation factor.
        feature_set: Restrict ``X`` to these columns (``Sequence``) or tag the
            full set with this name (``str``). ``None`` uses all columns and
            tags as ``"all"``.
        feature_set_name: Override for the feature-set tag when
            ``feature_set`` is a sequence (defaults to ``"custom"``).
        no_in_sample_tuning: Hard guardrail. ``True`` (default) means the
            sweep never inspects test-fold metrics for selection — it only
            reports them. Setting ``False`` is *only* for unit tests that
            need to assert the guardrail wiring; callers in production code
            must leave the flag at ``True``.

    Returns:
        DataFrame with one row per ``(fold, point_idx)``, columns covering
        fold geometry, grid-point overrides, the feature-set tag, and the
        per-fold metrics produced by
        :func:`tradedesk.ml.cv.fold_metrics_from_predictions`.

    Raises:
        ValueError: when ``X``/``y``/``forward_returns`` are misaligned, or
            ``no_in_sample_tuning`` is set to a non-bool.
    """
    if not isinstance(no_in_sample_tuning, bool):
        raise ValueError("no_in_sample_tuning must be a bool")
    if len(X) != len(y):
        raise ValueError(f"X and y length mismatch: {len(X)} vs {len(y)}")
    if not X.index.equals(y.index):
        raise ValueError("X and y must share the same index")
    if forward_returns is not None:
        if len(forward_returns) != len(X):
            raise ValueError("forward_returns must align with X")
        if not forward_returns.index.equals(X.index):
            raise ValueError("forward_returns must share index with X")

    base_config = base_config or DirectionClassifierConfig()
    grid = grid or ParamGrid()

    feature_tag, feature_cols = _coerce_feature_set(feature_set, X)
    if feature_set_name is not None and not isinstance(feature_set, str):
        feature_tag = feature_set_name
    X_feat = X[feature_cols]

    rows: list[_SweepFoldRecord] = []
    overrides_list = list(grid.iter_overrides())

    for fold in splitter.split(X_feat):
        X_train_full = X_feat.iloc[fold.train_idx]
        y_train_full = y.iloc[fold.train_idx]
        X_test_full = X_feat.iloc[fold.test_idx]
        y_test_full = y.iloc[fold.test_idx]

        train_mask = y_train_full.notna()
        test_mask = y_test_full.notna()
        if not train_mask.any() or not test_mask.any():
            continue

        X_train = X_train_full.loc[train_mask]
        y_train = y_train_full.loc[train_mask].astype(np.int64)
        X_test = X_test_full.loc[test_mask]
        y_test = y_test_full.loc[test_mask].astype(np.int64)

        carved = (
            make_validation_tail(X_train, y_train, validation_tail)
            if validation_tail is not None
            else None
        )

        for point_idx, overrides in enumerate(overrides_list):
            cfg = replace(base_config, **overrides)
            classifier = DirectionClassifier(config=cfg)
            if carved is not None:
                X_head, y_head, X_tail, y_tail = carved
                classifier.fit(X_head, y_head, eval_set=[(X_tail, y_tail)])
            else:
                classifier.fit(X_train, y_train)

            p_up = classifier.predict_proba(X_test)[:, 1]

            if forward_returns is not None:
                fr = forward_returns.iloc[fold.test_idx].loc[test_mask].to_numpy()
            else:
                fr = None

            metrics = fold_metrics_from_predictions(
                fold=fold.fold,
                n_train=int(train_mask.sum()),
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                y_true=y_test.to_numpy(),
                p_up=p_up,
                forward_returns=fr,
                threshold=threshold,
                periods_per_year=periods_per_year,
            )
            rows.append(
                _SweepFoldRecord(
                    fold=fold.fold,
                    point_idx=point_idx,
                    overrides=dict(overrides),
                    feature_set=feature_tag,
                    n_features=len(feature_cols),
                    metrics=metrics.as_dict(),
                )
            )

    if not rows:
        return pd.DataFrame()

    out: list[dict[str, Any]] = []
    for rec in rows:
        row: dict[str, Any] = {
            "fold": rec.fold,
            "point_idx": rec.point_idx,
            "feature_set": rec.feature_set,
            "n_features": rec.n_features,
        }
        row.update(rec.overrides)
        # Drop the duplicate `fold` from metrics dict — already in row.
        m = dict(rec.metrics)
        m.pop("fold", None)
        row.update(m)
        out.append(row)
    return pd.DataFrame(out).sort_values(["fold", "point_idx"]).reset_index(drop=True)


# ============================================================ feature pruning


def _gain_importances(model: DirectionClassifier) -> pd.Series:
    """Return the per-feature gain importance from a fitted DirectionClassifier.

    Falls back to zero-gain entries for features XGBoost dropped from the
    final tree set (``get_score`` only emits used features).
    """
    booster = model.model.get_booster()
    raw = booster.get_score(importance_type="gain")
    feature_names = booster.feature_names or []
    gains = pd.Series(
        {name: float(np.asarray(raw.get(name, 0.0)).item()) for name in feature_names},
        dtype=float,
    )
    return gains.sort_values(ascending=False)


def feature_importance_gain_pruning(
    X: pd.DataFrame,
    y: pd.Series,
    splitter: WalkForwardSplitter,
    *,
    base_config: DirectionClassifierConfig | None = None,
    drop_quantile: float = 0.25,
    forward_returns: pd.Series | None = None,
    threshold: float = 0.5,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
    validation_tail: ValidationTailSpec | None = None,
) -> pd.DataFrame:
    """Walk-forward gain pruning sweep.

    For each fold:

    1. Fit ``base_config`` on the (filtered) train slice.
    2. Compute XGBoost gain importance per feature.
    3. Drop the bottom-``drop_quantile`` features by gain.
    4. Refit on the same train slice, restricted to the surviving columns.
    5. Score both fits on the same test slice and emit two rows tagged
       ``variant ∈ {"full", "pruned"}``.

    The pruning happens *only* on training-fold gains so it never inspects
    the test fold — same hard guardrail as :func:`walk_forward_sweep`.

    Args:
        drop_quantile: Quantile of gain below which features are dropped.
            ``0.25`` keeps the top 75%. Must satisfy ``0 < drop_quantile < 1``.

    Returns:
        Tidy DataFrame with two rows per fold (``variant``); columns mirror
        :func:`walk_forward_sweep` plus ``variant`` and ``n_features_kept``.
    """
    if not 0.0 < drop_quantile < 1.0:
        raise ValueError("drop_quantile must satisfy 0 < drop_quantile < 1")
    if len(X) != len(y):
        raise ValueError(f"X and y length mismatch: {len(X)} vs {len(y)}")
    if not X.index.equals(y.index):
        raise ValueError("X and y must share the same index")

    base_config = base_config or DirectionClassifierConfig()

    rows: list[dict[str, Any]] = []
    for fold in splitter.split(X):
        X_train_full = X.iloc[fold.train_idx]
        y_train_full = y.iloc[fold.train_idx]
        X_test_full = X.iloc[fold.test_idx]
        y_test_full = y.iloc[fold.test_idx]

        train_mask = y_train_full.notna()
        test_mask = y_test_full.notna()
        if not train_mask.any() or not test_mask.any():
            continue

        X_train = X_train_full.loc[train_mask]
        y_train = y_train_full.loc[train_mask].astype(np.int64)
        X_test = X_test_full.loc[test_mask]
        y_test = y_test_full.loc[test_mask].astype(np.int64)

        if forward_returns is not None:
            fr = forward_returns.iloc[fold.test_idx].loc[test_mask].to_numpy()
        else:
            fr = None

        carved = (
            make_validation_tail(X_train, y_train, validation_tail)
            if validation_tail is not None
            else None
        )

        def _fit(features: list[str]) -> DirectionClassifier:
            classifier = DirectionClassifier(config=base_config)
            if carved is not None:
                X_head, y_head, X_tail, y_tail = carved
                classifier.fit(
                    X_head[features],
                    y_head,
                    eval_set=[(X_tail[features], y_tail)],
                )
            else:
                classifier.fit(X_train[features], y_train)
            return classifier

        full_features = list(X_train.columns)
        full_clf = _fit(full_features)
        gains = _gain_importances(full_clf)
        # Compute the cutoff over all features (zero-gain entries included),
        # so dropping is robust to zero-gain features XGBoost ignored.
        cutoff = float(np.quantile(np.asarray(gains.to_numpy(), dtype=float), drop_quantile))
        kept = [name for name in full_features if gains.get(name, 0.0) > cutoff]
        if not kept:
            kept = full_features  # cannot drop everything; degenerate guard
        pruned_clf = _fit(kept)

        for variant, classifier, features in (
            ("full", full_clf, full_features),
            ("pruned", pruned_clf, kept),
        ):
            p_up = classifier.predict_proba(X_test[features])[:, 1]
            metrics = fold_metrics_from_predictions(
                fold=fold.fold,
                n_train=int(train_mask.sum()),
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                y_true=y_test.to_numpy(),
                p_up=p_up,
                forward_returns=fr,
                threshold=threshold,
                periods_per_year=periods_per_year,
            ).as_dict()
            metrics.pop("fold", None)
            rows.append(
                {
                    "fold": fold.fold,
                    "variant": variant,
                    "n_features_kept": len(features),
                    "drop_quantile": drop_quantile if variant == "pruned" else 0.0,
                    **metrics,
                }
            )

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values(["fold", "variant"]).reset_index(drop=True)


# ============================================================ calibration sweep


@dataclass(frozen=True)
class _CalibrationFoldRow:
    fold: int
    n_train: int
    n_test: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    method: str
    brier_uncalibrated: float
    brier_calibrated: float
    reliability: pd.DataFrame = field(repr=False)


def walk_forward_calibration(
    X: pd.DataFrame,
    y: pd.Series,
    splitter: WalkForwardSplitter,
    *,
    base_config: DirectionClassifierConfig | None = None,
    method: Literal["platt", "isotonic"] = "platt",
    calibration_tail: ValidationTailSpec | None = None,
    n_reliability_bins: int = BRIER_RELIABILITY_DEFAULT_BINS,
) -> tuple[pd.DataFrame, dict[int, pd.DataFrame]]:
    """Per-fold probability calibration with Brier-score reporting.

    For each fold:

    1. Carve the train slice into ``(head, calibration_tail)`` using
       ``calibration_tail`` (defaults to ``ValidationTailSpec()`` —
       last 20% with no internal purge).
    2. Fit ``base_config`` on ``head``.
    3. Predict on the calibration tail, fit a calibrator (``platt`` or
       ``isotonic``), then predict on the test fold and apply the calibrator
       to those uncalibrated probabilities.
    4. Report Brier score before vs after calibration plus reliability bins
       on the test fold's calibrated probabilities.

    Args:
        calibration_tail: Tail spec. ``None`` is treated as
            ``ValidationTailSpec()``.
        method: ``"platt"`` (logistic regression on logit) or ``"isotonic"``.
        n_reliability_bins: Number of equal-width reliability bins.

    Returns:
        ``(summary, per_fold_reliability)`` where ``summary`` has one row per
        fold (Brier scores + fold geometry) and ``per_fold_reliability`` maps
        the fold index to the reliability DataFrame produced by
        :func:`reliability_bins` over the calibrated test predictions.
    """
    if len(X) != len(y):
        raise ValueError(f"X and y length mismatch: {len(X)} vs {len(y)}")
    if not X.index.equals(y.index):
        raise ValueError("X and y must share the same index")
    base_config = base_config or DirectionClassifierConfig()
    spec = calibration_tail or ValidationTailSpec()

    rows: list[_CalibrationFoldRow] = []
    for fold in splitter.split(X):
        X_train_full = X.iloc[fold.train_idx]
        y_train_full = y.iloc[fold.train_idx]
        X_test_full = X.iloc[fold.test_idx]
        y_test_full = y.iloc[fold.test_idx]

        train_mask = y_train_full.notna()
        test_mask = y_test_full.notna()
        if not train_mask.any() or not test_mask.any():
            continue

        X_train = X_train_full.loc[train_mask]
        y_train = y_train_full.loc[train_mask].astype(np.int64)
        X_test = X_test_full.loc[test_mask]
        y_test = y_test_full.loc[test_mask].astype(np.int64)

        carved = make_validation_tail(X_train, y_train, spec)
        if carved is None:
            # Fold too small for the configured calibration tail — skip.
            continue
        X_head, y_head, X_tail, y_tail = carved

        classifier = DirectionClassifier(config=base_config)
        classifier.fit(X_head, y_head, eval_set=[(X_tail, y_tail)])

        p_uncal_tail = classifier.predict_proba(X_tail)[:, 1]
        calibrator = fit_calibrator(method, p_uncal_tail, y_tail.to_numpy())

        p_uncal_test = classifier.predict_proba(X_test)[:, 1]
        p_cal_test = np.asarray(calibrator.transform(p_uncal_test), dtype=float)
        p_cal_test = np.clip(p_cal_test, 0.0, 1.0)

        y_true = y_test.to_numpy()
        b_before = brier_score(y_true, p_uncal_test)
        b_after = brier_score(y_true, p_cal_test)
        reliability = reliability_bins(y_true, p_cal_test, n_bins=n_reliability_bins)

        rows.append(
            _CalibrationFoldRow(
                fold=fold.fold,
                n_train=int(train_mask.sum()),
                n_test=int(test_mask.sum()),
                train_start=fold.train_start,
                train_end=fold.train_end,
                test_start=fold.test_start,
                test_end=fold.test_end,
                method=method,
                brier_uncalibrated=b_before,
                brier_calibrated=b_after,
                reliability=reliability,
            )
        )

    if not rows:
        return pd.DataFrame(), {}

    summary = pd.DataFrame(
        [
            {
                "fold": r.fold,
                "method": r.method,
                "n_train": r.n_train,
                "n_test": r.n_test,
                "train_start": r.train_start,
                "train_end": r.train_end,
                "test_start": r.test_start,
                "test_end": r.test_end,
                "brier_uncalibrated": r.brier_uncalibrated,
                "brier_calibrated": r.brier_calibrated,
                "brier_delta": r.brier_calibrated - r.brier_uncalibrated,
            }
            for r in rows
        ]
    ).set_index("fold")
    per_fold = {r.fold: r.reliability for r in rows}
    return summary, per_fold
