"""Machine-learning building blocks for tradedesk strategies.

The :mod:`tradedesk.ml` package collects feature engineering, label
engineering, walk-forward cross-validation, and model wrappers used by
ML-driven strategies (Phase 6).

ML dependencies (``xgboost``, ``scikit-learn``, ``joblib``) are declared as
the ``[ml]`` optional install::

    pip install 'tradedesk[ml]'

Importing this package does **not** require the ML extras —
:mod:`tradedesk.ml.features` and :mod:`tradedesk.ml.labels` only need
``pandas`` and ``numpy``. Sub-modules that depend on the ML extras
(e.g. :mod:`tradedesk.ml.model`) will raise a clear ``ImportError`` when
their backing library is missing.
"""

from .cv import (
    DEFAULT_PERIODS_PER_YEAR,
    FoldMetrics,
    FoldSplit,
    WalkForwardConfig,
    WalkForwardSplitter,
    aggregate_fold_metrics,
    fold_metrics_from_predictions,
    walk_forward_evaluate,
)
from .features import FeatureBuilder, FeatureConfig, default_indicator_stack
from .labels import (
    LabelConfig,
    TripleBarrierConfig,
    class_balance_report,
    forward_return_labels,
    print_class_balance,
    triple_barrier_labels,
)

__all__ = [
    "DEFAULT_PERIODS_PER_YEAR",
    "FeatureBuilder",
    "FeatureConfig",
    "FoldArtifacts",
    "FoldMetrics",
    "FoldSplit",
    "LabelConfig",
    "LeakageSanityResult",
    "TripleBarrierConfig",
    "WalkForwardConfig",
    "WalkForwardSplitter",
    "aggregate_feature_importance",
    "aggregate_fold_metrics",
    "aggregate_metrics_summary",
    "class_balance_report",
    "concatenated_equity_curve",
    "default_indicator_stack",
    "feature_importance_gains",
    "fold_metrics_from_predictions",
    "forward_return_labels",
    "plot_equity_curve",
    "print_class_balance",
    "render_markdown_report",
    "run_leakage_sanity",
    "triple_barrier_labels",
    "walk_forward_collect",
    "walk_forward_evaluate",
]


def __getattr__(name: str) -> object:
    """Lazy re-export of :mod:`tradedesk.ml.reporting`.

    Reporting depends on matplotlib + the ``[ml]`` extra (xgboost via
    :class:`DirectionClassifier`), so we only resolve those imports when the
    caller actually reaches into the reporting surface — keeping
    ``import tradedesk.ml`` cheap for callers that only want
    :mod:`features` / :mod:`labels`.
    """
    reporting_exports = {
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
    }
    if name in reporting_exports:
        from . import reporting

        return getattr(reporting, name)
    raise AttributeError(f"module 'tradedesk.ml' has no attribute {name!r}")
