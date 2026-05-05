"""Per-fold and aggregate reporting for walk-forward CV runs (Phase 6).

Consumes the fitted models and per-fold metrics produced by the harness in
:mod:`tradedesk.ml.cv` and renders:

* a markdown summary (per-fold table, mean ± std, feature-importance gains,
  leakage-sanity panel), and
* a matplotlib equity-curve PNG built from the concatenated out-of-sample
  predictions of every fold.

The reporting module is intentionally pure — it never hits disk on import,
never spawns processes, and matplotlib is forced onto the headless ``Agg``
backend so the report can be rendered from CI runners without an X server.

Design split with :mod:`tradedesk.ml.cv`
---------------------------------------

:func:`tradedesk.ml.cv.walk_forward_evaluate` returns a tidy metrics
DataFrame and discards the fitted models. That is the right contract for
the leakage-gate test surface but it cannot drive a report (no booster ⇒
no feature importance, no test-set probabilities ⇒ no equity curve).

:func:`walk_forward_collect` here mirrors the same loop but returns
:class:`FoldArtifacts` objects that carry the fitted
:class:`tradedesk.ml.model.DirectionClassifier`, the OOS probabilities,
and (when supplied) the realised forward returns aligned with the test
fold. Downstream rendering operates exclusively on those artefacts.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # noqa: E402 — must precede pyplot import for headless safety
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from tradedesk.ml.cv import (
    DEFAULT_PERIODS_PER_YEAR,
    FoldMetrics,
    WalkForwardSplitter,
    fold_metrics_from_predictions,
    walk_forward_evaluate,
)
from tradedesk.ml.model import DirectionClassifier  # noqa: E402

__all__ = [
    "FoldArtifacts",
    "LeakageSanityResult",
    "aggregate_feature_importance",
    "aggregate_metrics_summary",
    "concatenated_equity_curve",
    "feature_importance_gains",
    "plot_equity_curve",
    "render_markdown_report",
    "run_leakage_sanity",
    "walk_forward_collect",
]


# --------------------------------------------------------------------- artefacts


@dataclass(frozen=True)
class FoldArtifacts:
    """Bundle of per-fold artefacts the reporting module operates on.

    Attributes:
        metrics: The fold's :class:`FoldMetrics`.
        model: The fitted :class:`DirectionClassifier` for this fold.
        y_true: Binary {0, 1} ground-truth labels for the test fold (after
            NaN-label drop), aligned with ``test_index``.
        p_up: ``P(up)`` probabilities for the test fold, aligned with
            ``test_index``.
        forward_returns: Per-bar realised forward returns aligned with
            ``test_index``, or ``None`` if the harness was run without
            ``forward_returns``.
        test_index: The index of the test fold rows after the NaN-label
            drop. Preserves :class:`pandas.DatetimeIndex` when the input
            used datetimes.
        threshold: Probability threshold used to derive long/short/flat
            positions when constructing equity curves.
    """

    metrics: FoldMetrics
    model: DirectionClassifier
    y_true: np.ndarray
    p_up: np.ndarray
    forward_returns: np.ndarray | None
    test_index: pd.Index
    threshold: float = 0.5


@dataclass(frozen=True)
class LeakageSanityResult:
    """Verdict produced by :func:`run_leakage_sanity`.

    Carries the worst-case accuracy/AUC across folds plus the threshold the
    harness must clear to be considered a working leakage gate.
    """

    passed: bool
    min_accuracy: float
    min_auc: float
    n_folds: int
    threshold: float
    notes: str = ""


# ----------------------------------------------------------------- collect driver


def walk_forward_collect(
    X: pd.DataFrame,
    y: pd.Series,
    splitter: WalkForwardSplitter,
    model_factory: Callable[[], DirectionClassifier],
    *,
    forward_returns: pd.Series | None = None,
    threshold: float = 0.5,
    periods_per_year: int = DEFAULT_PERIODS_PER_YEAR,
) -> list[FoldArtifacts]:
    """Run walk-forward CV and retain the fitted model + OOS predictions per fold.

    Mirrors :func:`tradedesk.ml.cv.walk_forward_evaluate` argument-for-argument
    but returns :class:`FoldArtifacts` instead of a tidy metrics frame, so a
    downstream caller can render feature-importance gains, an equity curve, and
    fold-by-fold tables.
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

    artifacts: list[FoldArtifacts] = []
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
            fr_array: np.ndarray | None = (
                forward_returns.iloc[fold.test_idx].loc[test_mask].to_numpy()
            )
        else:
            fr_array = None

        metrics = fold_metrics_from_predictions(
            fold=fold.fold,
            n_train=int(train_mask.sum()),
            train_start=fold.train_start,
            train_end=fold.train_end,
            test_start=fold.test_start,
            test_end=fold.test_end,
            y_true=y_test.to_numpy(),
            p_up=p_up,
            forward_returns=fr_array,
            threshold=threshold,
            periods_per_year=periods_per_year,
        )
        artifacts.append(
            FoldArtifacts(
                metrics=metrics,
                model=model,
                y_true=y_test.to_numpy(),
                p_up=p_up,
                forward_returns=fr_array,
                test_index=X_test.index,
                threshold=threshold,
            )
        )
    return artifacts


# --------------------------------------------------------------- metric summary


def aggregate_metrics_summary(artifacts: Sequence[FoldArtifacts]) -> pd.DataFrame:
    """Mean and std (population) for each numeric :class:`FoldMetrics` field.

    Returns an empty DataFrame when ``artifacts`` is empty. Geometry columns
    (``train_start``, ``test_end`` …) are kept in the output so the caller can
    decide whether to render them.
    """
    if not artifacts:
        return pd.DataFrame()
    df = pd.DataFrame([a.metrics.as_dict() for a in artifacts]).set_index("fold")
    numeric = df.select_dtypes(include=[np.number])
    return pd.DataFrame(
        {"mean": numeric.mean(axis=0), "std": numeric.std(axis=0, ddof=0)}
    )


# ----------------------------------------------------------- feature importance


def feature_importance_gains(model: DirectionClassifier) -> pd.Series:
    """Per-feature importance *gain* from the underlying booster, sorted desc.

    Features that never appear in any split are omitted by XGBoost — callers
    should treat their gain as zero (which is what
    :func:`aggregate_feature_importance` does after a fillna).
    """
    booster = model.model.get_booster()
    raw: dict[str, Any] = booster.get_score(importance_type="gain")
    if not raw:
        return pd.Series(dtype=float, name="gain")
    s: pd.Series = pd.Series(raw, name="gain", dtype=float).sort_values(ascending=False)
    s.index.name = "feature"
    return s


def aggregate_feature_importance(
    artifacts: Sequence[FoldArtifacts],
) -> pd.DataFrame:
    """Mean and std of booster gains across folds, sorted by mean gain desc.

    Features absent from a fold's splits are imputed at zero gain for that
    fold (consistent with how XGBoost reports importance — *not* present
    means *not used*). The ``n_folds`` column counts how many folds actually
    used the feature so a glance shows whether a top-ranked feature was
    useful everywhere or just in one fold.
    """
    if not artifacts:
        return pd.DataFrame(columns=["mean_gain", "std_gain", "n_folds"])
    cols: list[pd.Series] = []
    for i, art in enumerate(artifacts):
        s = feature_importance_gains(art.model)
        s.name = f"fold_{i}"
        cols.append(s)
    if not any(len(s) for s in cols):
        return pd.DataFrame(columns=["mean_gain", "std_gain", "n_folds"])
    df = pd.concat(cols, axis=1).fillna(0.0)
    out = pd.DataFrame(
        {
            "mean_gain": df.mean(axis=1),
            "std_gain": df.std(axis=1, ddof=0),
            "n_folds": (df > 0.0).sum(axis=1).astype(int),
        }
    ).sort_values("mean_gain", ascending=False)
    out.index.name = "feature"
    return out


# ------------------------------------------------------------------- equity plot


def _positions_from_proba(p_up: np.ndarray, threshold: float) -> np.ndarray:
    pos = np.zeros_like(p_up, dtype=np.int64)
    pos[p_up >= threshold] = 1
    pos[p_up <= 1.0 - threshold] = -1
    return pos


def concatenated_equity_curve(
    artifacts: Sequence[FoldArtifacts],
) -> pd.Series:
    """Concatenate per-fold realised returns and return the cumulative curve.

    Folds without ``forward_returns`` are skipped silently (they cannot
    contribute to a realised PnL series). The result is sorted on
    ``test_index`` so a non-default fold ordering still produces a
    monotone curve in time.
    """
    pieces: list[pd.Series] = []
    for art in artifacts:
        if art.forward_returns is None:
            continue
        positions = _positions_from_proba(art.p_up, art.threshold)
        realised = positions.astype(float) * art.forward_returns.astype(float)
        pieces.append(pd.Series(realised, index=art.test_index))
    if not pieces:
        return pd.Series(dtype=float, name="equity")
    realised_series = pd.concat(pieces).sort_index()
    equity: pd.Series = realised_series.cumsum()
    equity.name = "equity"
    return equity


def plot_equity_curve(
    artifacts: Sequence[FoldArtifacts],
    output_path: str | Path,
    *,
    title: str = "Walk-forward OOS equity curve",
) -> Path:
    """Write a matplotlib equity-curve PNG and return the written path.

    Marks fold boundaries with light vertical guides so a reader can spot
    transitions between OOS segments. Renders a placeholder when no fold
    carried ``forward_returns`` so the image always exists for the markdown
    report.
    """
    output_path = Path(output_path)
    equity = concatenated_equity_curve(artifacts)
    fig, ax = plt.subplots(figsize=(10, 5))
    if equity.empty:
        ax.text(
            0.5,
            0.5,
            "no realised returns available",
            ha="center",
            va="center",
            transform=ax.transAxes,
        )
    else:
        ax.plot(equity.index, np.asarray(equity.values), linewidth=1.2, color="#0066cc")
        ax.axhline(0.0, color="#888", linewidth=0.8, linestyle="--")
        ax.set_xlabel("OOS sample")
        ax.set_ylabel("Cumulative realised return")
        # Light fold-boundary guides at the right edge of each fold (except the last).
        for art in list(artifacts)[:-1]:
            if art.forward_returns is None or len(art.test_index) == 0:
                continue
            ax.axvline(art.test_index[-1], color="#cccccc", linewidth=0.5)
    ax.set_title(title)
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, dpi=120)
    plt.close(fig)
    return output_path


# -------------------------------------------------------------- leakage sanity


def run_leakage_sanity(
    *,
    model_factory: Callable[[], DirectionClassifier],
    splitter: WalkForwardSplitter | None = None,
    n: int = 1500,
    seed: int = 0,
    leak_noise: float = 0.05,
    threshold_accuracy: float = 0.95,
) -> LeakageSanityResult:
    """Re-run the synthetic future-leak fixture against ``model_factory``.

    Constructs a random-walk close series with a 5-bar forward-return binary
    label and attaches a feature that *is* the label plus a sliver of noise.
    Walks the harness over the result and asserts the harness still measures
    near-perfect accuracy across every fold — proving the gate continues to
    catch any pipeline that pipes future information into a feature column.
    """
    h = 5
    rng = np.random.default_rng(seed)
    increments = rng.normal(0.0, 1.0, n + h)
    close = 100.0 + increments.cumsum()
    forward_return = close[h : n + h] - close[:n]
    y = pd.Series((forward_return > 0).astype(np.int64), name="y")
    leak = y.to_numpy().astype(float) + rng.normal(0.0, leak_noise, n)
    X = pd.DataFrame({"leak": leak}, index=pd.RangeIndex(n))

    cfg_splitter = (
        splitter
        if splitter is not None
        else WalkForwardSplitter(train_window=400, test_window=100, purge=h)
    )

    metrics_df = walk_forward_evaluate(X, y, cfg_splitter, model_factory)
    if metrics_df.empty:
        return LeakageSanityResult(
            passed=False,
            min_accuracy=float("nan"),
            min_auc=float("nan"),
            n_folds=0,
            threshold=threshold_accuracy,
            notes="harness produced 0 folds",
        )
    min_acc = float(metrics_df["accuracy"].min())
    min_auc = float(metrics_df["auc"].min())
    passed = min_acc >= threshold_accuracy and min_auc >= threshold_accuracy
    return LeakageSanityResult(
        passed=passed,
        min_accuracy=min_acc,
        min_auc=min_auc,
        n_folds=int(len(metrics_df)),
        threshold=threshold_accuracy,
        notes="" if passed else "harness no longer detects synthetic leak",
    )


# ----------------------------------------------------------------- markdown rendering


def _format_float(value: float, *, ndigits: int = 4) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "nan"
    return f"{value:.{ndigits}f}"


def _format_int(value: Any) -> str:
    return f"{int(value)}"


def _markdown_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
) -> str:
    """Render a small markdown table without pulling in tabulate."""
    if not rows:
        head = "| " + " | ".join(headers) + " |"
        sep = "| " + " | ".join("---" for _ in headers) + " |"
        return "\n".join([head, sep])
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for r in rows:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


_PER_FOLD_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fold", "int"),
    ("n_train", "int"),
    ("n_test", "int"),
    ("test_start", "int"),
    ("test_end", "int"),
    ("log_loss", "float"),
    ("accuracy", "float"),
    ("auc", "float"),
    ("hit_rate", "float"),
    ("sharpe", "float"),
    ("max_drawdown", "float"),
    ("trade_count", "int"),
)


def _per_fold_table(artifacts: Sequence[FoldArtifacts]) -> str:
    headers = [name for name, _ in _PER_FOLD_COLUMNS]
    rows: list[list[str]] = []
    for art in artifacts:
        d = art.metrics.as_dict()
        row: list[str] = []
        for name, kind in _PER_FOLD_COLUMNS:
            value = d[name]
            row.append(_format_int(value) if kind == "int" else _format_float(value))
        rows.append(row)
    return _markdown_table(headers, rows)


def _summary_table(summary: pd.DataFrame) -> str:
    if summary.empty:
        return "_no folds_"
    headers = ["metric", "mean ± std"]
    rows: list[list[str]] = []
    for metric in summary.index:
        m = float(summary.loc[metric, "mean"])
        s = float(summary.loc[metric, "std"])
        rows.append([str(metric), f"{_format_float(m)} ± {_format_float(s)}"])
    return _markdown_table(headers, rows)


def _feature_importance_table(fi: pd.DataFrame, top: int) -> str:
    if fi.empty:
        return "_no booster gains available_"
    head = fi.head(top)
    headers = ["feature", "mean_gain", "std_gain", "n_folds"]
    rows: list[list[str]] = []
    for feature in head.index:
        rows.append(
            [
                str(feature),
                _format_float(float(head.loc[feature, "mean_gain"])),
                _format_float(float(head.loc[feature, "std_gain"])),
                _format_int(head.loc[feature, "n_folds"]),
            ]
        )
    return _markdown_table(headers, rows)


def _leakage_panel(result: LeakageSanityResult | None) -> str:
    if result is None:
        return "_skipped — no result supplied_"
    verdict = "PASS" if result.passed else "FAIL"
    line = (
        f"**{verdict}** — synthetic future-leak fixture across {result.n_folds} folds. "
        f"min accuracy = {_format_float(result.min_accuracy)}, "
        f"min AUC = {_format_float(result.min_auc)} "
        f"(threshold ≥ {_format_float(result.threshold, ndigits=2)})."
    )
    if result.notes:
        line = line + "\n\n_Notes:_ " + result.notes
    return line


def render_markdown_report(
    artifacts: Sequence[FoldArtifacts],
    output_dir: str | Path,
    *,
    title: str = "Walk-forward CV report",
    leakage_result: LeakageSanityResult | None = None,
    feature_importance_top: int = 15,
    equity_curve_filename: str = "equity_curve.png",
    report_filename: str = "report.md",
) -> Path:
    """Render the full markdown report and return the written ``.md`` path.

    The function writes ``equity_curve_filename`` (a PNG) and
    ``report_filename`` (the markdown body) into ``output_dir``. The report
    embeds the equity-curve image with a relative path so it renders both
    on disk and when the directory is uploaded as an issue document.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    plot_equity_curve(artifacts, output_dir / equity_curve_filename)

    summary = aggregate_metrics_summary(artifacts)
    feature_importance = aggregate_feature_importance(artifacts)

    sections: list[str] = []
    sections.append(f"# {title}")
    sections.append("")
    sections.append(f"_Generated for {len(artifacts)} folds._")
    sections.append("")

    sections.append("## Per-fold metrics")
    sections.append("")
    sections.append(
        _per_fold_table(artifacts) if artifacts else "_no folds_"
    )
    sections.append("")

    sections.append("## Aggregate (mean ± std across folds)")
    sections.append("")
    sections.append(_summary_table(summary))
    sections.append("")

    sections.append("## Equity curve (concatenated OOS)")
    sections.append("")
    sections.append(f"![equity curve]({equity_curve_filename})")
    sections.append("")

    sections.append("## Feature importance (mean gain across folds)")
    sections.append("")
    sections.append(_feature_importance_table(feature_importance, feature_importance_top))
    sections.append("")

    sections.append("## Leakage sanity panel")
    sections.append("")
    sections.append(_leakage_panel(leakage_result))
    sections.append("")

    report_path = output_dir / report_filename
    report_path.write_text("\n".join(sections))
    return report_path
