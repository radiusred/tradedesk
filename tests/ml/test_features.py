"""Unit tests for :class:`tradedesk.ml.features.FeatureBuilder`."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from tradedesk.marketdata.indicators import ATR, RSI
from tradedesk.ml import FeatureBuilder, FeatureConfig, default_indicator_stack
from tradedesk.types import Candle


def _bars(n: int = 600, *, with_bid_ask: bool = False, seed: int = 0) -> pd.DataFrame:
    """Deterministic 1-min bar fixture: bounded random walk on log price."""
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


# ---------------------------------------------------------------- alignment


def test_transform_emits_aligned_dataframe() -> None:
    bars = _bars(500)
    builder = FeatureBuilder()
    out = builder.transform(bars)

    assert isinstance(out, pd.DataFrame)
    assert isinstance(out.index, pd.DatetimeIndex)
    assert out.index.is_monotonic_increasing
    # The warmup prefix is dropped — output is a contiguous suffix of the
    # input index aligned to the same right-hand bar.
    assert len(out) == len(bars) - builder.warmup()
    assert out.index[-1] == bars.index[-1]
    assert (out.index == bars.index[builder.warmup() :]).all()


def test_drop_warmup_removes_leading_nans() -> None:
    bars = _bars(800)
    out = FeatureBuilder().transform(bars)  # drop_warmup=True by default
    assert not out.isna().any().any(), "feature matrix has NaN after warmup drop"


def test_keep_warmup_preserves_index_length_and_pads_nan() -> None:
    bars = _bars(400)
    builder = FeatureBuilder(config=FeatureConfig(drop_warmup=False))
    out = builder.transform(bars)
    assert len(out) == len(bars)
    # Row 0 has NaN somewhere (rolling/indicator warmups not yet reached) —
    # but a few feature families (time, microstructure, VWAP) are well-
    # defined at every bar, so we don't require a fully-NaN row.
    assert out.iloc[0].isna().any()
    # Once warmup rows have elapsed, every column is non-NaN.
    assert not out.iloc[builder.warmup() :].isna().any().any()


def test_warmup_matches_max_of_vectorised_and_indicator_stack() -> None:
    builder = FeatureBuilder()
    indicator_warmup = max(ind.warmup_periods() for ind in builder.indicators.values())
    assert builder.warmup() == max(builder.config.vectorised_warmup(), indicator_warmup)


# ------------------------------------------------------------- known values


def test_lagged_log_return_known_value() -> None:
    """``log_ret_1`` at row t equals ``log(close[t]) - log(close[t-1])``.

    This is the spec's exit-criterion test: feeding a series with a known
    lagged-return value yields the expected feature column.
    """
    n = 200
    idx = pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC")
    close = pd.Series(np.linspace(100.0, 110.0, n), index=idx)
    bars = pd.DataFrame(
        {"open": close, "high": close + 0.1, "low": close - 0.1, "close": close},
        index=idx,
    )
    builder = FeatureBuilder(config=FeatureConfig(drop_warmup=False), indicators={})
    out = builder.transform(bars)

    expected = np.log(close.iloc[100] / close.iloc[99])
    assert math.isclose(out["log_ret_1"].iloc[100], expected, rel_tol=1e-9)

    # Multi-bar lookback for log_ret_5.
    expected_5 = np.log(close.iloc[100] / close.iloc[95])
    assert math.isclose(out["log_ret_5"].iloc[100], expected_5, rel_tol=1e-9)


def test_lagged_log_return_constant_growth_series() -> None:
    """A geometric series with constant 1% per-bar growth yields constant log returns."""
    n = 100
    growth = 1.01
    closes = np.array([100.0 * (growth ** i) for i in range(n)])
    bars = pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="1min", tz="UTC"),
    )
    builder = FeatureBuilder(
        config=FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(5,),
            include_time_features=False,
            include_microstructure=False,
            drop_warmup=False,
        ),
        indicators={},
    )
    out = builder.transform(bars)

    np.testing.assert_allclose(
        out["log_ret_1"].iloc[1:].to_numpy(),
        np.full(n - 1, math.log(growth)),
        rtol=1e-12,
    )
    np.testing.assert_allclose(
        out["log_ret_5"].iloc[5:].to_numpy(),
        np.full(n - 5, math.log(growth) * 5),
        rtol=1e-12,
    )


# -------------------------------------------------------------- no-look-ahead


def test_log_return_does_not_leak_future_via_nan_poisoning() -> None:
    """log_ret_w at t depends only on close[t] and close[t-w]; setting bars
    strictly after t to NaN must not change feature values at t."""
    bars = _bars(400)
    builder = FeatureBuilder(config=FeatureConfig(drop_warmup=False), indicators={})
    baseline = builder.transform(bars)

    perturbed = bars.copy()
    cut_idx = 250
    for col in ("open", "high", "low", "close", "volume"):
        perturbed.iloc[cut_idx + 1 :, perturbed.columns.get_loc(col)] = np.nan
    perturbed_out = builder.transform(perturbed)

    pd.testing.assert_series_equal(
        baseline["log_ret_5"].iloc[:cut_idx],
        perturbed_out["log_ret_5"].iloc[:cut_idx],
        check_names=False,
    )


def test_truncation_invariance_full_pipeline() -> None:
    """Truncating the input to row T does not change features at rows ≤ T.

    Canonical no-look-ahead test for the entire pipeline (vectorised
    features + indicator stack + microstructure).
    """
    bars = _bars(400, with_bid_ask=True, seed=4)
    cutoff = 350
    full = FeatureBuilder(config=FeatureConfig(drop_warmup=False)).transform(bars)
    trunc = FeatureBuilder(config=FeatureConfig(drop_warmup=False)).transform(
        bars.iloc[:cutoff]
    )

    pd.testing.assert_frame_equal(
        full.iloc[:cutoff],
        trunc,
        check_dtype=False,
    )


# --------------------------------------------------------- indicator parity


def test_indicator_stack_output_matches_streaming_indicator() -> None:
    """The ``rsi`` and ``atr`` columns equal the raw streaming indicator output.

    Feeding the same bars to a fresh :class:`RSI` and :class:`ATR` instance
    in chronological order must reproduce the values stored in the feature
    matrix — proving the FeatureBuilder doesn't subtly diverge from live.
    """
    bars = _bars(300, seed=2)
    builder = FeatureBuilder(
        config=FeatureConfig(
            return_windows=(1,),
            moment_windows=(5,),
            include_time_features=False,
            include_microstructure=False,
            drop_warmup=False,
        ),
        indicators={"rsi": RSI(period=14), "atr": ATR(period=14)},
    )
    out = builder.transform(bars)

    rsi = RSI(period=14)
    atr = ATR(period=14)
    expected_rsi: list[float | None] = []
    expected_atr: list[float | None] = []
    for ts, row in zip(bars.index, bars.itertuples(index=False), strict=True):
        candle = Candle(
            timestamp=str(ts),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        expected_rsi.append(rsi.update(candle))
        expected_atr.append(atr.update(candle))

    rsi_actual = out["rsi"].to_numpy()
    atr_actual = out["atr"].to_numpy()
    for i, (e_r, e_a) in enumerate(zip(expected_rsi, expected_atr, strict=True)):
        if e_r is None:
            assert math.isnan(rsi_actual[i])
        else:
            assert rsi_actual[i] == pytest.approx(e_r, rel=1e-12, abs=1e-12)
        if e_a is None:
            assert math.isnan(atr_actual[i])
        else:
            assert atr_actual[i] == pytest.approx(e_a, rel=1e-12, abs=1e-12)


def test_default_indicator_stack_emits_all_outputs() -> None:
    """Every default indicator contributes at least one column to the matrix."""
    bars = _bars(600)
    out = FeatureBuilder().transform(bars)

    expected_keys = {
        "adx",
        "adx_plus_di",
        "adx_minus_di",
        "atr",
        "bb_middle",
        "bb_upper",
        "bb_lower",
        "bb_std",
        "cci",
        "ema",
        "kc_middle",
        "kc_upper",
        "kc_lower",
        "macd",
        "macd_signal",
        "macd_histogram",
        "mfi",
        "obv",
        "rsi",
        "sma",
        "stoch_k",
        "stoch_d",
        "vwap",
        "williams_r",
    }
    missing = expected_keys - set(out.columns)
    assert not missing, f"missing indicator output columns: {sorted(missing)}"


def test_transform_is_idempotent_across_calls() -> None:
    """Calling :meth:`transform` twice yields bit-identical output.

    Guards against indicator state bleeding across calls.
    """
    bars = _bars(400, seed=3)
    builder = FeatureBuilder()
    a = builder.transform(bars)
    b = builder.transform(bars)
    pd.testing.assert_frame_equal(a, b)


def test_default_indicator_stack_returns_fresh_instances() -> None:
    a = default_indicator_stack()
    b = default_indicator_stack()
    assert a is not b
    assert a["rsi"] is not b["rsi"]
    assert set(a.keys()) == set(b.keys())


# ----------------------------------------------------------------- features


def test_microstructure_only_when_bid_ask_present() -> None:
    bars = _bars(300, with_bid_ask=False)
    out = FeatureBuilder().transform(bars)
    assert "spread" not in out.columns
    assert "spread_rel" not in out.columns

    bars_ba = _bars(300, with_bid_ask=True)
    out_ba = FeatureBuilder().transform(bars_ba)
    assert "spread" in out_ba.columns
    assert "spread_rel" in out_ba.columns
    # Synthetic spread is constant (0.02), so spread_rel == 0.02 / close.
    expected = 0.02 / bars_ba["close"].loc[out_ba.index]
    pd.testing.assert_series_equal(out_ba["spread_rel"], expected, check_names=False)


def test_microstructure_disabled_by_config() -> None:
    bars = _bars(300, with_bid_ask=True)
    out = FeatureBuilder(config=FeatureConfig(include_microstructure=False)).transform(bars)
    assert "body_range_ratio" not in out.columns
    assert "spread" not in out.columns


def test_time_features_unit_circle_invariant() -> None:
    bars = _bars(300)
    out = FeatureBuilder(config=FeatureConfig(drop_warmup=False)).transform(bars)
    np.testing.assert_allclose(
        (out["tod_sin"] ** 2 + out["tod_cos"] ** 2).to_numpy(),
        np.ones(len(bars)),
        rtol=1e-12,
        atol=1e-12,
    )
    assert out["weekday"].between(0, 6).all()


# --------------------------------------------------------------- validation


def test_validate_rejects_missing_columns() -> None:
    idx = pd.date_range("2024-01-01", periods=10, freq="1min", tz="UTC")
    bad = pd.DataFrame({"close": np.arange(10.0)}, index=idx)
    with pytest.raises(ValueError, match="requires columns"):
        FeatureBuilder().transform(bad)


def test_validate_rejects_non_datetime_index() -> None:
    idx = pd.RangeIndex(start=0, stop=50)
    bars = pd.DataFrame(
        {
            "open": np.zeros(50),
            "high": np.zeros(50),
            "low": np.zeros(50),
            "close": np.zeros(50),
        },
        index=idx,
    )
    with pytest.raises(ValueError, match="DatetimeIndex"):
        FeatureBuilder().transform(bars)


def test_validate_rejects_non_monotonic_index() -> None:
    bars = _bars(50)
    shuffled = bars.iloc[[5, 1, 2, 3, 4, 0] + list(range(6, 50))]
    with pytest.raises(ValueError, match="monotonically"):
        FeatureBuilder().transform(shuffled)
