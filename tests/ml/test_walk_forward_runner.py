"""Tests for :mod:`tradedesk.ml.walk_forward_runner` (Phase 6)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import zstandard as zstd

from tradedesk.ml.features import FeatureConfig
from tradedesk.ml.model import DirectionClassifierConfig
from tradedesk.ml.walk_forward_runner import (
    DEFAULT_HORIZONS,
    MINUTES_PER_TRADING_YEAR,
    WalkForwardRunConfig,
    _candle_path,
    _load_zst_csv,
    build_dataset,
    build_dataset_directional,
    load_dukascopy_bidask_minutes,
    run_walk_forward,
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


class TestBuildDatasetDirectional:
    """Spread-aware ``build_dataset_directional``."""

    def test_alignment_and_round_trip_relations(self) -> None:
        bars = _synthetic_minute_bars(800, seed=3, bid_ask_spread=2e-5)
        cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        X, y, fr_long, fr_short = build_dataset_directional(
            bars, horizon=15, feature_config=cfg
        )
        assert len(X) == len(y) == len(fr_long) == len(fr_short) > 0
        assert X.index.equals(y.index)
        assert X.index.equals(fr_long.index)
        assert X.index.equals(fr_short.index)
        assert set(np.unique(y)).issubset({0, 1})
        assert np.isfinite(fr_long.to_numpy()).all()
        assert np.isfinite(fr_short.to_numpy()).all()
        # If the legs were symmetric (no spread) fr_long + fr_short would be
        # ~0 everywhere; with a positive spread it must be strictly negative
        # on average because the round-trip cost shows up on both sides.
        assert (fr_long + fr_short).mean() < 0.0

    def test_horizon_strips_tail(self) -> None:
        bars = _synthetic_minute_bars(400, seed=4)
        cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        X_h15, *_ = build_dataset_directional(bars, horizon=15, feature_config=cfg)
        X_h60, *_ = build_dataset_directional(bars, horizon=60, feature_config=cfg)
        assert len(X_h15) - len(X_h60) == pytest.approx(60 - 15, abs=1)

    def test_missing_bidask_columns_raises(self) -> None:
        bars = _synthetic_minute_bars(64, seed=5).drop(columns=["bid_close"])
        with pytest.raises(ValueError, match="bid_close"):
            build_dataset_directional(bars, horizon=15)


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

    def test_spread_aware_default_off(self) -> None:
        cfg = WalkForwardRunConfig()
        assert cfg.spread_aware is False


class TestBuildDatasetHorizonValidation:
    """Both dataset builders reject horizon < 1 before doing any work."""

    def test_build_dataset_rejects_horizon_zero(self) -> None:
        bars = _synthetic_minute_bars(64, seed=10)
        with pytest.raises(ValueError, match="horizon must be >= 1"):
            build_dataset(bars, horizon=0)

    def test_build_dataset_rejects_negative_horizon(self) -> None:
        bars = _synthetic_minute_bars(64, seed=11)
        with pytest.raises(ValueError, match="horizon must be >= 1"):
            build_dataset(bars, horizon=-3)

    def test_build_dataset_directional_rejects_horizon_zero(self) -> None:
        bars = _synthetic_minute_bars(64, seed=12)
        with pytest.raises(ValueError, match="horizon must be >= 1"):
            build_dataset_directional(bars, horizon=0)


# ============================================================ Dukascopy I/O


def _write_zst_candles(
    path: Path,
    minutes: int,
    *,
    base: float,
    seed: int,
    start: pd.Timestamp | None = None,
) -> None:
    """Write a Dukascopy-style zstd-compressed daily candle file."""
    rng = np.random.default_rng(seed)
    closes = base + np.cumsum(rng.normal(0.0, 5e-5, minutes))
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0.0, 5e-5, minutes))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0.0, 5e-5, minutes))
    if start is None:
        start = pd.Timestamp("2024-01-01T00:00:00", tz="UTC")
    timestamps = pd.date_range(
        start=start,
        periods=minutes,
        freq="1min",
        tz="UTC",
    )
    df = pd.DataFrame(
        {
            "timestamp": timestamps.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(minutes, 100.0),
        }
    )
    csv_bytes = df.to_csv(index=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    cctx = zstd.ZstdCompressor()
    path.write_bytes(cctx.compress(csv_bytes))


def _make_dukascopy_cache(
    cache_dir: Path,
    symbol: str,
    day: date,
    minutes: int = 60,
) -> None:
    """Drop a single-day bid+ask cache pair for ``symbol`` at ``day``."""
    bid_path = _candle_path(cache_dir, symbol, day, "bid")
    ask_path = _candle_path(cache_dir, symbol, day, "ask")
    start = pd.Timestamp(day, tz="UTC")
    _write_zst_candles(
        bid_path, minutes, base=1.10, seed=int(day.toordinal()), start=start
    )
    _write_zst_candles(
        ask_path, minutes, base=1.10005, seed=int(day.toordinal()) + 9, start=start
    )


class TestCandlePath:
    """`_candle_path` mirrors Dukascopy's month-zero-indexed layout."""

    def test_january_is_month_zero(self, tmp_path: Path) -> None:
        p = _candle_path(tmp_path, "EURUSD", date(2024, 1, 5), "bid")
        assert p.relative_to(tmp_path) == Path("EURUSD/2024/00/05_bid.csv.zst")

    def test_december_is_month_eleven(self, tmp_path: Path) -> None:
        p = _candle_path(tmp_path, "GBPUSD", date(2024, 12, 31), "ask")
        assert p.relative_to(tmp_path) == Path("GBPUSD/2024/11/31_ask.csv.zst")


class TestLoadZstCsv:
    """`_load_zst_csv` decompresses, parses, and indexes a Dukascopy file."""

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _load_zst_csv(tmp_path / "does_not_exist.csv.zst") is None

    def test_returns_none_on_corrupted_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "corrupt.csv.zst"
        bad.write_bytes(b"this is not a zstd stream")
        assert _load_zst_csv(bad) is None

    def test_decompresses_and_indexes_by_timestamp(self, tmp_path: Path) -> None:
        path = tmp_path / "EURUSD" / "2024" / "00" / "05_bid.csv.zst"
        _write_zst_candles(path, minutes=10, base=1.10, seed=42)

        df = _load_zst_csv(path)

        assert df is not None
        assert len(df) == 10
        assert df.index.name == "timestamp"
        assert isinstance(df.index, pd.DatetimeIndex)
        assert df.index.tz is not None
        assert {"open", "high", "low", "close", "volume"}.issubset(df.columns)


class TestLoadDukascopyBidaskMinutes:
    """`load_dukascopy_bidask_minutes` joins bid+ask files day-by-day."""

    def test_loads_aligned_bid_ask_frame(self, tmp_path: Path) -> None:
        symbol = "EURUSD"
        day = date(2024, 1, 5)
        _make_dukascopy_cache(tmp_path, symbol, day, minutes=60)

        bars = load_dukascopy_bidask_minutes(tmp_path, symbol, day, day)

        assert len(bars) == 60
        for col in ("open", "high", "low", "close", "volume", "bid_close", "ask_close"):
            assert col in bars.columns
        # Mid close is the midpoint of bid/ask close.
        assert ((bars["close"] - 0.5 * (bars["bid_close"] + bars["ask_close"])).abs() < 1e-9).all()
        # Volume is the sum of bid + ask legs.
        assert (bars["volume"] == 200.0).all()
        # High picks the per-row max across legs; low picks the per-row min.
        assert (bars["high"] >= bars["close"]).all()
        assert (bars["low"] <= bars["close"]).all()

    def test_skips_days_with_only_bid_or_only_ask(self, tmp_path: Path) -> None:
        symbol = "EURUSD"
        d_full = date(2024, 1, 5)
        d_bid_only = date(2024, 1, 6)
        _make_dukascopy_cache(tmp_path, symbol, d_full, minutes=10)
        # Day 6: only the bid file exists.
        _write_zst_candles(
            _candle_path(tmp_path, symbol, d_bid_only, "bid"),
            minutes=10,
            base=1.10,
            seed=99,
        )

        bars = load_dukascopy_bidask_minutes(tmp_path, symbol, d_full, d_bid_only)

        assert len(bars) == 10  # only the fully-paired day survives

    def test_raises_when_no_bid_ask_data_in_range(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No bid/ask data"):
            load_dukascopy_bidask_minutes(
                tmp_path, "EURUSD", date(2024, 1, 1), date(2024, 1, 3)
            )


# ============================================================ run_walk_forward


def _populate_cache_for_run(
    cache_dir: Path,
    symbol: str,
    *,
    n_days: int,
    minutes_per_day: int = 1440,
) -> tuple[date, date]:
    start = date(2024, 1, 1)
    for i in range(n_days):
        d = start + timedelta(days=i)
        _make_dukascopy_cache(cache_dir, symbol, d, minutes=minutes_per_day)
    return start, start + timedelta(days=n_days - 1)


class TestRunWalkForward:
    """End-to-end smoke for :func:`run_walk_forward`.

    Uses a tiny synthetic Dukascopy cache + a deliberately small train/test
    window so the run finishes in a few seconds without xgboost early stopping
    kicking in.
    """

    @pytest.fixture
    def fast_run_config(self) -> WalkForwardRunConfig:
        # Tight model so the test stays fast.
        model_cfg = DirectionClassifierConfig(
            n_estimators=20,
            max_depth=3,
            learning_rate=0.2,
            early_stopping_rounds=None,
            seed=7,
            n_jobs=1,
        )
        feat_cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        return WalkForwardRunConfig(
            symbol="EURUSD",
            date_from=date(2024, 1, 1),
            date_to=date(2024, 1, 4),
            horizons=(15,),
            train_window_bars=600,
            test_window_bars=200,
            embargo_factor=1,
            threshold=0.55,
            feature_config=feat_cfg,
            model_config=model_cfg,
            label_neutral_band=0.0,
            spread_aware=False,
        )

    def test_runs_end_to_end_mid_price(
        self, tmp_path: Path, fast_run_config: WalkForwardRunConfig
    ) -> None:
        _populate_cache_for_run(tmp_path, "EURUSD", n_days=4)

        result = run_walk_forward(tmp_path, fast_run_config)

        assert result.config is fast_run_config
        assert not result.bars.empty
        assert set(result.per_horizon_metrics.keys()) == {15}
        metrics = result.per_horizon_metrics[15]
        assert not metrics.empty
        for col in ("accuracy", "auc", "log_loss", "sharpe", "trade_count"):
            assert col in metrics.columns

    def test_runs_end_to_end_spread_aware(
        self, tmp_path: Path, fast_run_config: WalkForwardRunConfig
    ) -> None:
        _populate_cache_for_run(tmp_path, "EURUSD", n_days=4)
        spread_cfg = WalkForwardRunConfig(
            symbol=fast_run_config.symbol,
            date_from=fast_run_config.date_from,
            date_to=fast_run_config.date_to,
            horizons=fast_run_config.horizons,
            train_window_bars=fast_run_config.train_window_bars,
            test_window_bars=fast_run_config.test_window_bars,
            embargo_factor=fast_run_config.embargo_factor,
            threshold=fast_run_config.threshold,
            feature_config=fast_run_config.feature_config,
            model_config=fast_run_config.model_config,
            label_neutral_band=0.0,
            spread_aware=True,
        )

        result = run_walk_forward(tmp_path, spread_cfg)

        assert spread_cfg.spread_aware is True
        metrics = result.per_horizon_metrics[15]
        assert not metrics.empty
        # Sharpe column is present even when no trades cross the threshold.
        assert "sharpe" in metrics.columns

    def test_uses_default_config_when_omitted(self, tmp_path: Path) -> None:
        # `run_walk_forward(cache_dir)` falls back to the default config.
        # Default config asks for 8 years of EURUSD which obviously is not
        # in the empty cache — assert the loader raises instead of silently
        # producing nothing, proving the default-config branch is reached.
        with pytest.raises(ValueError, match="No bid/ask data"):
            run_walk_forward(tmp_path)


# ============================================================ package surface


class TestPackageSurface:
    """`tradedesk.ml.__getattr__` lazily resolves reporting exports."""

    def test_reporting_export_resolves_lazily(self) -> None:
        import tradedesk.ml as ml

        # Top-level attribute defined in the lazy table; resolves to the
        # reporting submodule's class.
        assert ml.FoldArtifacts is not None
        assert ml.LeakageSanityResult is not None

    def test_unknown_attribute_raises(self) -> None:
        import tradedesk.ml as ml

        with pytest.raises(AttributeError, match="no attribute"):
            _ = ml.this_attr_does_not_exist
