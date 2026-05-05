"""Unit tests for :class:`tradedesk.ml.model.DirectionClassifier`."""

from __future__ import annotations

import builtins
import importlib
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from tradedesk.ml.model import DirectionClassifier, DirectionClassifierConfig


def _xy(n: int = 400, n_features: int = 8, *, seed: int = 0) -> tuple[pd.DataFrame, pd.Series]:
    """Synthetic binary classification fixture with a learnable signal."""
    rng = np.random.default_rng(seed)
    X = rng.normal(0.0, 1.0, size=(n, n_features))
    # Binary label driven by a linear combination of the first three features
    # plus noise — XGBoost should fit the signal but not memorise the noise.
    logits = 0.7 * X[:, 0] - 0.5 * X[:, 1] + 0.3 * X[:, 2] + rng.normal(0, 0.4, size=n)
    y = (logits > 0).astype(int)
    cols = [f"f{i}" for i in range(n_features)]
    return pd.DataFrame(X, columns=cols), pd.Series(y, name="up")


def _fast_config() -> DirectionClassifierConfig:
    """Tiny estimator so the unit-test suite stays sub-second."""
    return DirectionClassifierConfig(
        n_estimators=20,
        max_depth=3,
        learning_rate=0.1,
        early_stopping_rounds=None,
        seed=7,
        n_jobs=1,
    )


# ----------------------------------------------------------------- determinism


def test_predict_proba_is_deterministic_for_same_seed() -> None:
    X, y = _xy()
    cfg = _fast_config()

    a = DirectionClassifier(cfg).fit(X, y)
    b = DirectionClassifier(cfg).fit(X, y)

    proba_a = a.predict_proba(X)
    proba_b = b.predict_proba(X)

    assert proba_a.shape == (len(X), 2)
    np.testing.assert_array_equal(proba_a, proba_b)


def test_different_seed_changes_predictions() -> None:
    X, y = _xy()
    a = DirectionClassifier(DirectionClassifierConfig(seed=1, **_fast_kwargs())).fit(X, y)
    b = DirectionClassifier(DirectionClassifierConfig(seed=2, **_fast_kwargs())).fit(X, y)
    # Different seeds drive different bagging/column subsamples → outputs differ.
    assert not np.array_equal(a.predict_proba(X), b.predict_proba(X))


def _fast_kwargs() -> dict[str, object]:
    """Fast-config kwargs with the seed left to the caller."""
    return {
        "n_estimators": 20,
        "max_depth": 3,
        "learning_rate": 0.1,
        "early_stopping_rounds": None,
        "n_jobs": 1,
    }


# ------------------------------------------------------------- save/load round-trip


def test_save_load_roundtrips_predictions(tmp_path: Path) -> None:
    X, y = _xy()
    clf = DirectionClassifier(_fast_config()).fit(X, y)
    expected = clf.predict_proba(X)

    artefact = tmp_path / "model.joblib"
    clf.save(artefact)
    assert artefact.exists() and artefact.stat().st_size > 0

    restored = DirectionClassifier.load(artefact)
    actual = restored.predict_proba(X)

    np.testing.assert_allclose(actual, expected, rtol=0, atol=1e-12)
    assert restored.config == clf.config


def test_save_unfitted_raises(tmp_path: Path) -> None:
    clf = DirectionClassifier(_fast_config())
    with pytest.raises(RuntimeError, match="unfitted"):
        clf.save(tmp_path / "nope.joblib")


def test_predict_proba_unfitted_raises() -> None:
    clf = DirectionClassifier(_fast_config())
    with pytest.raises(RuntimeError, match="not fitted"):
        clf.predict_proba(pd.DataFrame({"f0": [0.0]}))


def test_load_rejects_foreign_payload(tmp_path: Path) -> None:
    import joblib

    artefact = tmp_path / "junk.joblib"
    joblib.dump({"schema_version": 999, "model": object()}, artefact)
    with pytest.raises(ValueError, match="Unsupported DirectionClassifier"):
        DirectionClassifier.load(artefact)


# --------------------------------------------------------- eval_set / early stopping


def test_fit_accepts_eval_set_and_early_stopping() -> None:
    X, y = _xy(n=500)
    X_train, X_val = X.iloc[:400], X.iloc[400:]
    y_train, y_val = y.iloc[:400], y.iloc[400:]

    cfg = DirectionClassifierConfig(
        n_estimators=200,
        max_depth=3,
        learning_rate=0.1,
        early_stopping_rounds=10,
        seed=11,
        n_jobs=1,
    )
    clf = DirectionClassifier(cfg).fit(X_train, y_train, eval_set=[(X_val, y_val)])

    # Early stopping must have stopped at-or-before the configured budget.
    best_iter = clf.model.best_iteration
    assert isinstance(best_iter, int)
    assert 0 <= best_iter < cfg.n_estimators


# ------------------------------------------------------- pandas-dtype isolation


def test_saved_artefact_does_not_pickle_dataframes(tmp_path: Path) -> None:
    """The persisted payload must not retain any pandas containers."""
    import joblib

    X, y = _xy()
    clf = DirectionClassifier(_fast_config()).fit(X, y)
    artefact = tmp_path / "model.joblib"
    clf.save(artefact)
    payload = joblib.load(artefact)

    # Walk the top-level payload — none of the values should be a pandas type.
    for key, value in payload.items():
        assert not isinstance(value, (pd.DataFrame, pd.Series, pd.Index)), (
            f"payload[{key!r}] is a pandas object: {type(value).__name__}"
        )


# --------------------------------------------------------- ML-extra import gate


def test_model_import_raises_when_xgboost_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the [ml] extra, importing the model module must explain how to fix it."""
    real_import = builtins.__import__

    def _block_xgboost(name: str, *args: object, **kwargs: object) -> object:
        if name == "xgboost" or name.startswith("xgboost."):
            raise ModuleNotFoundError("No module named 'xgboost'")
        return real_import(name, *args, **kwargs)

    # Drop any cached module so the import statement re-runs the gating block.
    for mod in [m for m in sys.modules if m == "xgboost" or m.startswith("xgboost.")]:
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.delitem(sys.modules, "tradedesk.ml.model", raising=False)
    monkeypatch.setattr(builtins, "__import__", _block_xgboost)

    with pytest.raises(ImportError, match=r"\[ml\] extra"):
        importlib.import_module("tradedesk.ml.model")
