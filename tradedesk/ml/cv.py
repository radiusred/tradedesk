"""Walk-forward cross-validation harness with embargo/purge (Phase 6 / RAD-896).

This module is the **leakage-gate keystone** of the Phase 6 sprint. It exposes
a positional :class:`WalkForwardSplitter` that respects the standard López de
Prado guarantees against information leakage when labels carry a forward
horizon ``h``:

* **purge** — drop training rows whose label evaluation window overlaps the
  test window. With label horizon ``h`` this is at least ``h`` samples at the
  tail of every train fold.
* **embargo** — additional buffer between train end and test start beyond
  ``purge``, intended to neutralise feature serial-autocorrelation that
  extends *past* the label horizon. This mirrors MdLP's *Advances in
  Financial Machine Learning*, §7.4.2.

The two parameters are **additive**: the gap between the last training row
and the first test row is exactly ``embargo + purge`` samples.

Per-fold metrics are computed by :func:`fold_metrics_from_predictions`; the
full sweep is driven by :func:`walk_forward_evaluate`. Metrics produced for
each fold:

* ``log_loss`` — binary cross-entropy
* ``accuracy`` — fraction of correct binary predictions at ``threshold``
* ``auc`` — area under the ROC curve (rank-based)
* ``hit_rate`` — accuracy restricted to "actionable" predictions, i.e.
  rows where ``max(p, 1-p) >= threshold``
* ``sharpe`` — annualised Sharpe of the realised fold returns (NaN when
  forward returns aren't supplied)
* ``max_drawdown`` — peak-to-trough drawdown of cumulative fold returns
* ``trade_count`` — number of non-flat positions

Leakage gate
------------

The accompanying tests (``tests/ml/test_cv.py``) assert two things:

1. With a feature that *literally* encodes the label, the harness reports
   high accuracy across folds — proving that XGBoost will exploit any leak
   we feed it and that the harness is sensitive enough to catch it.
2. With a pure-noise feature, the harness reports ~50% accuracy across
   folds — proving the splitter / metrics aggregator do not invent edges.

The embargo/purge boundary contract is verified sample-by-sample in
``test_purge_drops_overlapping_train_rows`` so the temporal-leakage
mechanism stays correct independent of any specific dataset.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from typing import Any, Final, Protocol

import numpy as np
import pandas as pd

#: Trading days per year used for Sharpe annualisation by default.
DEFAULT_PERIODS_PER_YEAR: Final[int] = 252

#: Numeric floor used when clipping probabilities for the log-loss formula.
_LOG_LOSS_EPS: Final[float] = 1e-15


__all__ = [
    "DEFAULT_PERIODS_PER_YEAR",
    "FitPredictModel",
    "FoldMetrics",
    "FoldSplit",
    "WalkForwardConfig",
    "WalkForwardSplitter",
    "aggregate_fold_metrics",
    "fold_metrics_from_predictions",
    "walk_forward_evaluate",
]


# --------------------------------------------------------------- splitter config


@dataclass(frozen=True)
class WalkForwardConfig:
    """Configuration for :class:`WalkForwardSplitter`.

    Attributes:
        train_window: Number of samples in each training fold. Ignored when
            ``expanding`` is ``True``. Must be ``>= 1``.
        test_window: Number of samples in each test fold. Must be ``>= 1``.
        step: Stride between consecutive test starts. Defaults to
            ``test_window`` (non-overlapping test folds).
        embargo: Buffer between train end and test start *beyond* ``purge``,
            in samples. Use to absorb feature serial-autocorrelation that
            extends past the label horizon. Must be ``>= 0``.
        purge: Number of samples at the tail of each training fold to drop
            because their label window would overlap the test fold. Set to
            the label horizon ``h`` (or ``h - 1`` if labels are right-open).
            Must be ``>= 0``.
        expanding: When ``True`` every train fold starts at index ``0``
            (anchored walk-forward). When ``False`` (default) the train
            window slides.
    """

    train_window: int
    test_window: int
    step: int | None = None
    embargo: int = 0
    purge: int = 0
    expanding: bool = False

    def __post_init__(self) -> None:
        if self.train_window < 1:
            raise ValueError("train_window must be >= 1")
        if self.test_window < 1:
            raise ValueError("test_window must be >= 1")
        if self.step is not None and self.step < 1:
            raise ValueError("step must be >= 1 when set")
        if self.embargo < 0:
            raise ValueError("embargo must be >= 0")
        if self.purge < 0:
            raise ValueError("purge must be >= 0")

    @property
    def effective_step(self) -> int:
        """Stride used between test starts (defaults to ``test_window``)."""
        return self.step if self.step is not None else self.test_window

    @property
    def gap(self) -> int:
        """Total samples removed between train end and test start."""
        return self.embargo + self.purge


@dataclass(frozen=True)
class FoldSplit:
    """One walk-forward fold expressed as positional integer index arrays.

    Both arrays are zero-based positions into the input sequence, *not*
    timestamps. ``train_idx`` is contiguous over
    ``[train_start, train_start + len(train_idx))`` and ``test_idx`` is
    contiguous over ``[test_start, test_start + len(test_idx))``.
    """

    fold: int
    train_idx: np.ndarray
    test_idx: np.ndarray

    @property
    def train_start(self) -> int:
        return int(self.train_idx[0])

    @property
    def train_end(self) -> int:
        """Exclusive upper bound of ``train_idx``."""
        return int(self.train_idx[-1]) + 1

    @property
    def test_start(self) -> int:
        return int(self.test_idx[0])

    @property
    def test_end(self) -> int:
        """Exclusive upper bound of ``test_idx``."""
        return int(self.test_idx[-1]) + 1

    @property
    def gap(self) -> int:
        """Sample gap between ``train_end`` and ``test_start`` (inclusive)."""
        return self.test_start - self.train_end


# ----------------------------------------------------------------------- splitter


class WalkForwardSplitter:
    """Yield :class:`FoldSplit` objects for walk-forward cross-validation.

    The splitter is positional — it works on any sequence whose length
    can be measured (``DataFrame``, ``Series``, ``DatetimeIndex``, ``int``).
    Output ``FoldSplit`` objects carry zero-based integer positions and the
    caller is responsible for translating those into label/feature slices
    (typically ``X.iloc[fold.train_idx]`` / ``y.iloc[fold.train_idx]``).
    """

    def __init__(
        self,
        config: WalkForwardConfig | None = None,
        **kwargs: Any,
    ) -> None:
        if config is not None and kwargs:
            raise TypeError("pass either config= or kwargs, not both")
        self.config = config if config is not None else WalkForwardConfig(**kwargs)

    def __repr__(self) -> str:
        c = self.config
        return (
            f"WalkForwardSplitter(train_window={c.train_window}, "
            f"test_window={c.test_window}, step={c.effective_step}, "
            f"embargo={c.embargo}, purge={c.purge}, expanding={c.expanding})"
        )

    def n_splits(self, n: int | object) -> int:
        """Number of folds produced for an input of length ``n``."""
        return sum(1 for _ in self._yield_starts(self._extract_n(n)))

    def split(self, X: object) -> Iterator[FoldSplit]:
        """Yield :class:`FoldSplit` for each fold.

        Args:
            X: Anything supporting ``len()``, an ``int`` sample count, or a
                :class:`pandas.DatetimeIndex`. ``DatetimeIndex`` is also
                checked for monotonicity.

        Yields:
            One :class:`FoldSplit` per fold, starting from the earliest fold
            that admits at least one training row.

        Raises:
            ValueError: when no fold can be produced (insufficient samples
                given the configured windows / embargo / purge).
        """
        n = self._extract_n(X)
        cfg = self.config
        produced = 0
        for fold_idx, test_start in enumerate(self._yield_starts(n)):
            test_end = test_start + cfg.test_window
            train_end = test_start - cfg.gap
            if train_end <= 0:
                # Not enough room before the gap for any train sample.
                continue
            if cfg.expanding:
                train_start = 0
            else:
                train_start = max(0, train_end - cfg.train_window)
            if train_end - train_start < 1:
                continue
            train_idx = np.arange(train_start, train_end, dtype=np.int64)
            test_idx = np.arange(test_start, test_end, dtype=np.int64)
            yield FoldSplit(fold=fold_idx, train_idx=train_idx, test_idx=test_idx)
            produced += 1
        if produced == 0:
            raise ValueError(
                f"WalkForwardSplitter produced 0 folds for n={n} with {self!r}; "
                "increase n or relax window/embargo/purge."
            )

    def _yield_starts(self, n: int) -> Iterator[int]:
        cfg = self.config
        # Both modes require at least train_window training samples in the
        # first fold. Sliding holds train at exactly train_window; expanding
        # uses train_window as the minimum and grows from there.
        min_first = cfg.train_window + cfg.gap
        max_test_start = n - cfg.test_window
        if max_test_start < min_first:
            return
        for t in range(min_first, max_test_start + 1, cfg.effective_step):
            yield t

    @staticmethod
    def _extract_n(X: object) -> int:
        if isinstance(X, bool):
            raise TypeError("cannot derive sample count from bool")
        if isinstance(X, int):
            return X
        if isinstance(X, pd.DatetimeIndex) and not X.is_monotonic_increasing:
            raise ValueError("DatetimeIndex must be monotonically increasing")
        try:
            return len(X)  # type: ignore[arg-type]
        except TypeError as exc:  # pragma: no cover - defensive
            raise TypeError(
                f"cannot derive sample count from {type(X).__name__}"
            ) from exc


# ------------------------------------------------------------------- metric core


@dataclass(frozen=True)
class FoldMetrics:
    """Per-fold metrics emitted by :func:`walk_forward_evaluate`.

    The first four fields are the fold geometry; the remainder are the
    quality / risk metrics. Sharpe and drawdown are NaN when the caller
    omits ``forward_returns``.
    """

    fold: int
    n_train: int
    n_test: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    log_loss: float
    accuracy: float
    auc: float
    hit_rate: float
    sharpe: float
    max_drawdown: float
    trade_count: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class FitPredictModel(Protocol):
    """Minimal model interface required by :func:`walk_forward_evaluate`."""

    def fit(self, X: pd.DataFrame, y: pd.Series) -> Any: ...
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray: ...


def _binary_log_loss(y_true: np.ndarray, p_up: np.ndarray) -> float:
    p = np.clip(p_up, _LOG_LOSS_EPS, 1.0 - _LOG_LOSS_EPS)
    return float(-np.mean(y_true * np.log(p) + (1.0 - y_true) * np.log(1.0 - p)))


def _binary_accuracy(y_true: np.ndarray, p_up: np.ndarray, threshold: float) -> float:
    y_pred = (p_up >= threshold).astype(np.int64)
    return float(np.mean(y_pred == y_true))


def _hit_rate(y_true: np.ndarray, p_up: np.ndarray, threshold: float) -> float:
    """Accuracy restricted to "actionable" predictions.

    A prediction is *actionable* when ``max(p, 1-p) >= threshold`` — i.e.
    the model is confident enough to take a position. Returns NaN when no
    predictions are actionable.
    """
    confident = np.maximum(p_up, 1.0 - p_up) >= threshold
    if not confident.any():
        return float("nan")
    y_pred = (p_up >= 0.5).astype(np.int64)
    return float(np.mean(y_pred[confident] == y_true[confident]))


def _binary_auc(y_true: np.ndarray, p_up: np.ndarray) -> float:
    """Rank-based ROC AUC with average-rank tie handling."""
    n_pos = int(y_true.sum())
    n_neg = int(len(y_true) - n_pos)
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    ranks = pd.Series(p_up).rank(method="average").to_numpy()
    sum_pos_ranks = float(ranks[y_true == 1].sum())
    auc = (sum_pos_ranks - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)
    return float(auc)


def _annualised_sharpe(returns: np.ndarray, periods_per_year: int) -> float:
    if returns.size == 0:
        return float("nan")
    mu = float(np.mean(returns))
    sigma = float(np.std(returns, ddof=1)) if returns.size > 1 else 0.0
    if sigma == 0.0:
        return float("nan")
    return float(np.sqrt(periods_per_year) * mu / sigma)


def _max_drawdown(returns: np.ndarray) -> float:
    if returns.size == 0:
        return float("nan")
    equity = np.cumsum(returns)
    peak = np.maximum.accumulate(equity)
    drawdown = equity - peak
    return float(drawdown.min())


def _positions_from_proba(p_up: np.ndarray, threshold: float) -> np.ndarray:
    """Long/short/flat position from a probability vector.

    +1 when ``p_up >= threshold``, -1 when ``p_up <= 1 - threshold``, else 0.
    With ``threshold == 0.5`` the policy reduces to "long when p_up > 0.5,
    short otherwise" with a single flat row at exactly 0.5.
    """
    pos = np.zeros_like(p_up, dtype=np.int64)
    pos[p_up >= threshold] = 1
    pos[p_up <= 1.0 - threshold] = -1
    return pos


def fold_metrics_from_predictions(
    *,
    fold: int,
    n_train: int,
    test_start: int,
    test_end: int,
    train_start: int,
    train_end: int,
    y_true: np.ndarray,
    p_up: np.ndarray,
    forward_returns: np.ndarray | None = None,
    threshold: float = 0.5,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> FoldMetrics:
    """Materialise a :class:`FoldMetrics` from raw arrays.

    Args:
        fold: Zero-based fold index.
        n_train, train_start, train_end, test_start, test_end: Fold geometry.
        y_true: Binary {0, 1} ground-truth labels for the test fold.
        p_up: ``P(up)`` probabilities for the test fold (typically
            ``model.predict_proba(X_test)[:, 1]``).
        forward_returns: Optional per-bar forward return series aligned with
            ``y_true``, used to compute Sharpe / drawdown / trade count
            assuming a long/short/flat policy gated by ``threshold``.
        threshold: Probability threshold for the actionability gate. Must
            satisfy ``0.5 <= threshold <= 1``.
        periods_per_year: Sharpe annualisation factor.

    Returns:
        A :class:`FoldMetrics` dataclass. Sharpe / drawdown / trade-count
        fields are NaN / 0 when ``forward_returns`` is ``None``.
    """
    if not 0.5 <= threshold <= 1.0:
        raise ValueError("threshold must satisfy 0.5 <= threshold <= 1")
    if y_true.shape != p_up.shape:
        raise ValueError(
            f"y_true and p_up must share shape; got {y_true.shape} vs {p_up.shape}"
        )

    y_true_int = y_true.astype(np.int64)
    n_test = int(y_true_int.size)

    log_loss = _binary_log_loss(y_true_int, p_up)
    accuracy = _binary_accuracy(y_true_int, p_up, threshold=0.5)
    auc = _binary_auc(y_true_int, p_up)
    hit_rate = _hit_rate(y_true_int, p_up, threshold=threshold)

    if forward_returns is None:
        sharpe = float("nan")
        max_dd = float("nan")
        trade_count = 0
    else:
        if forward_returns.shape != y_true.shape:
            raise ValueError(
                "forward_returns must align with y_true; got "
                f"{forward_returns.shape} vs {y_true.shape}"
            )
        positions = _positions_from_proba(p_up, threshold=threshold)
        realised = positions.astype(float) * forward_returns.astype(float)
        # Strip flat rows from Sharpe so it represents per-trade return distribution.
        traded = realised[positions != 0]
        sharpe = _annualised_sharpe(traded, periods_per_year=periods_per_year)
        max_dd = _max_drawdown(realised)
        trade_count = int((positions != 0).sum())

    return FoldMetrics(
        fold=fold,
        n_train=n_train,
        n_test=n_test,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        log_loss=log_loss,
        accuracy=accuracy,
        auc=auc,
        hit_rate=hit_rate,
        sharpe=sharpe,
        max_drawdown=max_dd,
        trade_count=trade_count,
    )


# ------------------------------------------------------------------- aggregator


def aggregate_fold_metrics(metrics: list[FoldMetrics]) -> pd.DataFrame:
    """Convert a list of :class:`FoldMetrics` to a tidy DataFrame.

    Index is ``fold``; columns preserve the dataclass field order. Suitable
    for printing or for downstream :func:`pandas.DataFrame.describe` /
    :func:`pandas.DataFrame.mean` aggregation.
    """
    if not metrics:
        return pd.DataFrame()
    df = pd.DataFrame([m.as_dict() for m in metrics])
    return df.set_index("fold").sort_index()


# ----------------------------------------------------------------------- driver


def walk_forward_evaluate(
    X: pd.DataFrame,
    y: pd.Series,
    splitter: WalkForwardSplitter,
    model_factory: Callable[[], FitPredictModel],
    *,
    forward_returns: pd.Series | None = None,
    threshold: float = 0.5,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> pd.DataFrame:
    """Run walk-forward evaluation and return per-fold metrics.

    For each fold:

    1. Slice ``(X.iloc[train_idx], y.iloc[train_idx])`` and call
       ``model_factory()`` to obtain a *fresh* model.
    2. ``model.fit(X_train, y_train)``.
    3. ``p_up = model.predict_proba(X_test)[:, 1]``.
    4. Build a :class:`FoldMetrics` row from ``(y_test, p_up)``.

    Rows whose label is ``NaN`` are dropped from each fold *after* the split
    so that purge/embargo are still applied positionally on the original
    index. A fold is skipped (with no metric row) when either side ends up
    empty after the NaN drop.

    Args:
        X: Feature matrix with a unique :class:`pandas.RangeIndex` or
            :class:`pandas.DatetimeIndex` aligned with ``y``.
        y: Binary-label series aligned with ``X``. NaN entries are dropped
            per fold.
        splitter: A configured :class:`WalkForwardSplitter`.
        model_factory: Zero-argument callable returning a fresh, unfitted
            object satisfying :class:`FitPredictModel`.
        forward_returns: Optional per-bar realised forward return series
            aligned with ``X``, used to populate Sharpe / drawdown / trade
            count.
        threshold: Probability threshold passed through to
            :func:`fold_metrics_from_predictions`.
        periods_per_year: Sharpe annualisation factor.

    Returns:
        DataFrame indexed by fold with one row per executed fold. Empty
        when every fold collapsed to zero usable rows.

    Raises:
        ValueError: ``X`` and ``y`` (or ``forward_returns``) are not
            aligned, or the splitter produces no folds.
    """
    if len(X) != len(y):
        raise ValueError(f"X and y length mismatch: {len(X)} vs {len(y)}")
    if not X.index.equals(y.index):
        raise ValueError("X and y must share the same index")
    if forward_returns is not None:
        if len(forward_returns) != len(X):
            raise ValueError("forward_returns must align with X")
        if not forward_returns.index.equals(X.index):
            raise ValueError("forward_returns must share index with X")

    rows: list[FoldMetrics] = []
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

        model = model_factory()
        model.fit(X_train, y_train)
        proba = np.asarray(model.predict_proba(X_test))
        if proba.ndim != 2 or proba.shape[1] != 2:
            raise ValueError(
                f"model.predict_proba must return (n, 2); got shape {proba.shape}"
            )
        p_up = proba[:, 1]

        if forward_returns is not None:
            fr = forward_returns.iloc[fold.test_idx].loc[test_mask].to_numpy()
        else:
            fr = None

        rows.append(
            fold_metrics_from_predictions(
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
        )

    return aggregate_fold_metrics(rows)
