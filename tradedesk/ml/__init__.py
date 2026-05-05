"""Machine-learning building blocks for tradedesk strategies.

The :mod:`tradedesk.ml` package collects feature engineering, label
engineering, walk-forward cross-validation, and model wrappers used by
ML-driven strategies (Phase 6 / RAD-896).

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
    "FoldMetrics",
    "FoldSplit",
    "LabelConfig",
    "TripleBarrierConfig",
    "WalkForwardConfig",
    "WalkForwardSplitter",
    "aggregate_fold_metrics",
    "class_balance_report",
    "default_indicator_stack",
    "fold_metrics_from_predictions",
    "forward_return_labels",
    "print_class_balance",
    "triple_barrier_labels",
    "walk_forward_evaluate",
]
