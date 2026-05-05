"""Tests for :mod:`tradedesk.ml.walk_forward_runner` (Phase 6 / RAD-903)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from tradedesk.ml.features import FeatureConfig
from tradedesk.ml.walk_forward_runner import (
    DEFAULT_HORIZONS,
    MINUTES_PER_TRADING_YEAR,
    WalkForwardRunConfig,
    build_dataset,
)


def _synthetic_minute_bars(
    n: int,
    seed: int = 0,
    start: str = "2024-01-01T00:00:00",
    bid_ask_spread: float = 1e-5,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    base = 1.10
    closes = []
    for _ in range(n):
        step = float(rng.normal(0.0, 5e-5))
        base = base * (1.0 + step)
        closes.append(base)
    closes_arr = np.asarray(closes, dtype=float)
    highs = closes_arr * (1.0 + np.abs(rng.normal(0.0, 5e-5, size=n)))
    lows = closes_arr * (1.0 - np.abs(rng.normal(0.0, 5e-5, size=n)))
    opens = np.concatenate([[closes_arr[0]], closes_arr[:-1]])
    half = bid_ask_spread / 2.0
    timestamps = pd.date_range(start=start, periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, highs),
            "low": np.minimum(opens, lows),
            "close": closes_arr,
            "volume": np.full(n, 100.0),
            "bid_close": closes_arr - half,
            "ask_close": closes_arr + half,
        },
        index=timestamps,
    )


class TestBuildDataset:
    """`build_dataset` must produce aligned (X, y, fr) with the right shapes."""

    def test_basic_alignment(self) -> None:
        bars = _synthetic_minute_bars(800, seed=0)
        cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        X, y, fr = build_dataset(bars, horizon=15, feature_config=cfg)

        assert len(X) == len(y) == len(fr) > 0
        assert X.index.equals(y.index)
        assert X.index.equals(fr.index)
        # Binary labels.
        assert set(np.unique(y)).issubset({0, 1})
        # Forward returns are finite.
        assert np.isfinite(fr.to_numpy()).all()

    def test_horizon_strips_tail(self) -> None:
        # With horizon=h, the trailing h rows of the feature matrix have NaN
        # forward returns and are dropped.
        bars = _synthetic_minute_bars(400, seed=1)
        cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        X_h15, _, _ = build_dataset(bars, horizon=15, feature_config=cfg)
        X_h60, _, _ = build_dataset(bars, horizon=60, feature_config=cfg)
        assert len(X_h15) - len(X_h60) == pytest.approx(60 - 15, abs=1)

    def test_neutral_band_filters_small_moves(self) -> None:
        # A large neutral band should map nearly every label to "down" (0)
        # since with synthetic data the forward return rarely exceeds 1%.
        bars = _synthetic_minute_bars(400, seed=2)
        cfg = FeatureConfig(
            return_windows=(1,),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        _, y_band, _ = build_dataset(
            bars, horizon=15, feature_config=cfg, label_neutral_band=0.01
        )
        assert (y_band == 0).all() or (y_band.mean() < 0.05)


class TestWalkForwardRunConfigDefaults:
    """Sanity-check the default config exposed publicly."""

    def test_defaults_match_phase6_spec(self) -> None:
        cfg = WalkForwardRunConfig()
        assert cfg.symbol == "EURUSD"
        assert cfg.horizons == DEFAULT_HORIZONS
        assert cfg.threshold == 0.55
        assert cfg.periods_per_year == MINUTES_PER_TRADING_YEAR
        # Train window > test window so each fold gets enough data.
        assert cfg.train_window_bars >= cfg.test_window_bars

    def test_defaults_use_default_horizons(self) -> None:
        assert DEFAULT_HORIZONS == (15, 60)
