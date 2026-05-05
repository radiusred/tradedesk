"""Unit tests for :class:`tradedesk.ml.features.FeatureBuilder`."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradedesk.ml import FeatureBuilder, FeatureConfig


def _bars(n: int = 600, *, with_bid_ask: bool = False, seed: int = 0) -> pd.DataFrame:
    """Deterministic 1-min bar fixture: random walk with bounded TR."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0, 0.0005, size=n)
    close = 100.0 * np.exp(np.cumsum(steps))
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0, 0.05, size=n)
    low = np.minimum(open_, close) - rng.uniform(0, 0.05, size=n)
    volume = rng.integers(100, 1000, size=n).astype(float)
    idx = pd.date_range("2024-01-01 00:00", periods=n, freq="1min", tz="UTC")
    df = pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )
    if with_bid_ask:
        df["bid_close"] = close - 0.01
        df["ask_close"] = close + 0.01
    return df


def test_transform_emits_aligned_dataframe():
    bars = _bars(500)
    builder = FeatureBuilder()
    out = builder.transform(bars)
    assert isinstance(out, pd.DataFrame)
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.is_monotonic_increasing
    # The warmup prefix is dropped — output is a contiguous suffix of the input
    # index, never longer than it.
    assert len(out) <= len(bars)
    assert out.index[-1] == bars.index[-1]


def test_drop_warmup_removes_leading_nans():
    bars = _bars(800)
    builder = FeatureBuilder()  # drop_warmup=True by default
    out = builder.transform(bars)
    # No NaNs allowed once the warmup prefix is dropped.
    assert not out.isna().any().any(), "feature matrix has NaN after warmup drop"


def test_keep_warmup_preserves_index_length():
    bars = _bars(400)
    cfg = FeatureConfig(drop_warmup=False)
    out = FeatureBuilder(cfg).transform(bars)
    assert len(out) == len(bars)
    # Leading rows must contain NaNs that an explicit caller can decide on.
    assert out.iloc[: cfg.warmup()].isna().any().any()


def test_lagged_log_return_known_value():
    """A 1-bar lagged log return equals log(close_t / close_{t-1})."""
    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = pd.Series(np.linspace(100.0, 110.0, n), index=idx)
    bars = pd.DataFrame(
        {"open": close, "high": close + 0.1, "low": close - 0.1, "close": close},
        index=idx,
    )
    cfg = FeatureConfig(drop_warmup=False)
    out = FeatureBuilder(cfg).transform(bars)
    expected = np.log(close.iloc[100] / close.iloc[99])
    assert math.isclose(out["log_ret_1"].iloc[100], expected, rel_tol=1e-9)


def test_lagged_return_does_not_leak_future():
    """log_ret_w at t depends only on close[t] and close[t-w]; setting any
    bar after t to NaN must not change the value at t."""
    bars = _bars(400)
    builder = FeatureBuilder(FeatureConfig(drop_warmup=False))
    baseline = builder.transform(bars)

    perturbed = bars.copy()
    cut_idx = 250
    # Wreck every bar strictly after cut_idx.
    perturbed.iloc[cut_idx + 1 :, perturbed.columns.get_loc("close")] = np.nan
    perturbed.iloc[cut_idx + 1 :, perturbed.columns.get_loc("open")] = np.nan
    perturbed.iloc[cut_idx + 1 :, perturbed.columns.get_loc("high")] = np.nan
    perturbed.iloc[cut_idx + 1 :, perturbed.columns.get_loc("low")] = np.nan
    perturbed_out = builder.transform(perturbed)

    pd.testing.assert_series_equal(
        baseline["log_ret_5"].iloc[:cut_idx],
        perturbed_out["log_ret_5"].iloc[:cut_idx],
        check_names=False,
    )


def test_microstructure_only_when_bid_ask_present():
    bars = _bars(300, with_bid_ask=False)
    out = FeatureBuilder().transform(bars)
    assert "spread" not in out.columns
    assert "spread_rel" not in out.columns

    bars_ba = _bars(300, with_bid_ask=True)
    out_ba = FeatureBuilder().transform(bars_ba)
    assert "spread" in out_ba.columns
    assert "spread_rel" in out_ba.columns
    # Synthetic spread is constant (0.02), so spread_rel must equal 0.02 / close.
    expected = 0.02 / bars_ba["close"].loc[out_ba.index]
    pd.testing.assert_series_equal(out_ba["spread_rel"], expected, check_names=False)


def test_time_features_cyclical_bounds():
    bars = _bars(300)
    out = FeatureBuilder().transform(bars)
    assert out["tod_sin"].between(-1.0, 1.0).all()
    assert out["tod_cos"].between(-1.0, 1.0).all()
    assert out["weekday"].between(0, 6).all()


def test_validate_rejects_missing_columns():
    idx = pd.date_range("2024-01-01", periods=10, freq="1min", tz="UTC")
    bad = pd.DataFrame({"close": np.arange(10.0)}, index=idx)
    with pytest.raises(ValueError, match="requires columns"):
        FeatureBuilder().transform(bad)


def test_validate_rejects_non_monotonic_index():
    bars = _bars(50)
    shuffled = bars.iloc[[5, 1, 2, 3, 4, 0] + list(range(6, 50))]
    with pytest.raises(ValueError, match="monotonically increasing"):
        FeatureBuilder().transform(shuffled)


def test_extra_columns_passed_through():
    bars = _bars(200)
    bars["regime_flag"] = 1.0
    cfg = FeatureConfig(extra_columns=("regime_flag",))
    out = FeatureBuilder(cfg).transform(bars)
    assert "regime_flag" in out.columns
    assert (out["regime_flag"] == 1.0).all()
