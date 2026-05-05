"""XGBoost direction classifier wrapper (Phase 6).

A thin, opinionated wrapper around :class:`xgboost.XGBClassifier` that fixes
hyperparameter defaults, enforces deterministic training, and persists models
through :mod:`joblib` without leaking pandas dtypes into the saved artefact.

The wrapper is intentionally narrow: it exposes ``fit``, ``predict_proba``,
``save``, and ``load``. Anything that needs the full sklearn surface area
(custom callbacks, GPU training, multi-class targets) should reach for
:class:`xgboost.XGBClassifier` directly.

Importing this module requires the ``[ml]`` extra
(``pip install 'tradedesk[ml]'``). The optional dependencies are
import-time-gated and produce a clear :class:`ImportError` mentioning the
extra when missing — rather than the bare ``ModuleNotFoundError`` that bubbles
up from a deep transitive import.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

try:
    import joblib
    import xgboost as xgb
except ImportError as exc:  # pragma: no cover - exercised via stubbed import
    raise ImportError(
        "tradedesk.ml.model requires the [ml] extra. "
        "Install with: pip install 'tradedesk[ml]'"
    ) from exc


__all__ = ["DirectionClassifier", "DirectionClassifierConfig"]


@dataclass(frozen=True)
class DirectionClassifierConfig:
    """Hyperparameters for :class:`DirectionClassifier`.

    Defaults are tuned for 1-minute FX bar features (Phase 6 sprint): shallow
    trees with strong regularisation and aggressive sub-sampling to combat the
    noise-to-signal ratio. ``n_jobs=1`` is the default because XGBoost's
    ``hist`` method can introduce small numerical non-determinism across runs
    when reductions are parallelised; single-threaded training preserves the
    bit-exact determinism the unit tests rely on.
    """

    max_depth: int = 4
    min_child_weight: float = 1.0
    gamma: float = 0.0
    reg_lambda: float = 1.0
    learning_rate: float = 0.05
    n_estimators: int = 500
    early_stopping_rounds: int | None = 50
    subsample: float = 0.8
    colsample_bytree: float = 0.8
    seed: int = 42
    n_jobs: int = 1
    objective: str = "binary:logistic"
    eval_metric: str = "logloss"
    tree_method: str = "hist"

    def to_xgb_params(self) -> dict[str, Any]:
        """Translate the dataclass into kwargs for :class:`xgb.XGBClassifier`.

        ``seed`` maps to sklearn's ``random_state``; ``early_stopping_rounds``
        is dropped when ``None`` so it doesn't override the estimator default.
        """
        params = asdict(self)
        params["random_state"] = params.pop("seed")
        if params["early_stopping_rounds"] is None:
            params.pop("early_stopping_rounds")
        return params


class DirectionClassifier:
    """Binary direction classifier built on :class:`xgb.XGBClassifier`.

    Parameters
    ----------
    config:
        Hyperparameter dataclass. Defaults are taken from
        :class:`DirectionClassifierConfig`.

    Notes
    -----
    The wrapper retains the trained ``xgb.XGBClassifier`` plus the immutable
    config. Persistence via :meth:`save`/:meth:`load` round-trips both —
    DataFrames passed to :meth:`fit`/:meth:`predict_proba` are never stored,
    so the saved artefact is portable across pandas releases.
    """

    def __init__(self, config: DirectionClassifierConfig | None = None) -> None:
        self.config = config or DirectionClassifierConfig()
        self._model: xgb.XGBClassifier | None = None

    @property
    def model(self) -> xgb.XGBClassifier:
        """The fitted underlying estimator. Raises if :meth:`fit` was not called."""
        if self._model is None:
            raise RuntimeError(
                "DirectionClassifier is not fitted yet — call fit(X, y) first."
            )
        return self._model

    def fit(
        self,
        X: pd.DataFrame | np.ndarray,
        y: pd.Series | np.ndarray,
        *,
        eval_set: Sequence[tuple[pd.DataFrame | np.ndarray, pd.Series | np.ndarray]]
        | None = None,
    ) -> DirectionClassifier:
        """Fit the underlying ``xgb.XGBClassifier`` on ``(X, y)``.

        ``eval_set`` is forwarded verbatim and enables XGBoost's early-stopping
        loop when ``config.early_stopping_rounds`` is set; when no eval set is
        provided we strip the early-stopping rounds so XGBoost doesn't error.
        Returns ``self`` so the call chains.
        """
        params = self.config.to_xgb_params()
        if eval_set is None:
            params.pop("early_stopping_rounds", None)
        model = xgb.XGBClassifier(**params)
        model.fit(X, y, eval_set=eval_set, verbose=False)
        self._model = model
        return self

    def predict_proba(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        """Return the ``(n_samples, 2)`` probability matrix for ``[down, up]``."""
        return np.asarray(self.model.predict_proba(X))

    def save(self, path: str | Path) -> None:
        """Persist ``(config, fitted_estimator)`` to ``path`` via :mod:`joblib`.

        The persisted dict contains the immutable config dataclass plus the
        XGBoost estimator pickled by joblib. XGBClassifier itself stores only
        primitives (numpy arrays, the booster, feature-name strings) once
        fitted — no pandas DataFrames are retained — so the artefact is
        portable across pandas releases.
        """
        if self._model is None:
            raise RuntimeError("Cannot save an unfitted DirectionClassifier.")
        joblib.dump(
            {"schema_version": 1, "config": self.config, "model": self._model},
            path,
        )

    @classmethod
    def load(cls, path: str | Path) -> DirectionClassifier:
        """Re-hydrate a classifier previously written by :meth:`save`."""
        payload = joblib.load(path)
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("config"), DirectionClassifierConfig)
        ):
            raise ValueError(
                f"Unsupported DirectionClassifier artefact at {path!r}: "
                f"expected schema_version=1 payload, got {type(payload).__name__}."
            )
        instance = cls(config=payload["config"])
        instance._model = payload["model"]
        return instance
