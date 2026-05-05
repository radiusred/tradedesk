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
    "FeatureBuilder",
    "FeatureConfig",
    "default_indicator_stack",
    "LabelConfig",
    "TripleBarrierConfig",
    "class_balance_report",
    "forward_return_labels",
    "print_class_balance",
    "triple_barrier_labels",
]
