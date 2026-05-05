"""Machine-learning building blocks for tradedesk strategies.

The :mod:`tradedesk.ml` package collects feature engineering, label engineering,
walk-forward cross-validation, and model wrappers used by ML-driven strategies
(see Phase 6 / RAD-896).

ML dependencies (``xgboost``, ``scikit-learn``, ``joblib``) are declared as the
``[ml]`` optional install:

    pip install 'tradedesk[ml]'

Importing this package does **not** require the ML extras — sub-modules that
need them (e.g. :mod:`tradedesk.ml.model`) raise a clear ``ImportError`` with
install hint when their backing library is missing.
"""

from .features import FeatureBuilder, FeatureConfig

__all__ = ["FeatureBuilder", "FeatureConfig"]
