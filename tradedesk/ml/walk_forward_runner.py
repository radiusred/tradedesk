"""End-to-end walk-forward evaluator for the Phase 6 ML stack.

Glues together the four Phase 6 building blocks —
:class:`tradedesk.ml.FeatureBuilder`,
:func:`tradedesk.ml.forward_return_labels`,
:class:`tradedesk.ml.DirectionClassifier`, and
:func:`tradedesk.ml.walk_forward_evaluate` — and feeds them a Dukascopy
bid/ask cache so the resulting per-fold OOS Sharpe table can be used as
the Phase 6 go/no-go signal.

The runner is intentionally framework-light: no async, no broker, no
recording. The result is a tidy ``pandas.DataFrame`` per horizon you can
print, persist, or hand to a notebook.

Caveats baked in:

* Sharpe is annualised at ``n_minutes_per_year = 252 * 24 * 60`` (the
  industry-standard 24/7 minute-bar convention) and is computed from
  overlapping ``h``-bar forward returns, so it will be inflated by
  roughly ``sqrt(h)`` versus a non-overlapping benchmark. For the
  first-pass go/no-go this matters less than the *sign* of the Sharpe.
* Forward returns are mid close-to-close — no spread cost is applied to
  the realised P&L. A loosely spread-aware label can be requested via
  ``label_neutral_band`` (e.g. ``0.0001`` ≈ 1bp); ask-to-bid round-trip
  labels are available via :func:`tradedesk.ml.forward_return_labels`
  with ``spread_aware=True`` but are not the default here.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd
import zstandard as zstd

from .cv import WalkForwardConfig, WalkForwardSplitter, walk_forward_evaluate
from .features import FeatureBuilder, FeatureConfig
from .labels import LabelConfig, forward_return_labels
from .model import DirectionClassifier, DirectionClassifierConfig

log = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_HORIZONS",
    "MINUTES_PER_TRADING_YEAR",
    "WalkForwardRunConfig",
    "WalkForwardRunResult",
    "build_dataset",
    "build_dataset_directional",
    "load_dukascopy_bidask_minutes",
    "run_walk_forward",
]


#: Forward-return horizons (in 1-min bars) used by the first-pass run.
DEFAULT_HORIZONS: Final[tuple[int, ...]] = (15, 60)

#: 1-min bars per trading year used for Sharpe annualisation
#: (252 trading days × 24 hours × 60 minutes — the conventional 24/7 minute
#: bar count, slightly conservative for FX which trades 24×5).
MINUTES_PER_TRADING_YEAR: Final[int] = 252 * 24 * 60


# ------------------------------------------------------------------ data loading


def _load_zst_csv(path: Path) -> pd.DataFrame | None:
    """Decompress and parse a Dukascopy daily ``.csv.zst`` candle file.

    Mirrors :func:`tradedesk.execution.backtest.dukascopy._load_daily_candles`
    without the .csv ramdisk fast-path — the runner reads each file once.
    """
    if not path.exists():
        return None
    try:
        dctx = zstd.ZstdDecompressor()
        with open(path, "rb") as f_in:
            with dctx.stream_reader(f_in) as reader:
                df = pd.read_csv(
                    io.TextIOWrapper(io.BufferedReader(reader), encoding="utf-8"),
                )
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df.set_index("timestamp")
    except Exception:
        log.warning("Failed to read Dukascopy candle file %s", path, exc_info=True)
        return None


def _candle_path(cache_dir: Path, symbol: str, day: date, side: str) -> Path:
    month_0 = f"{day.month - 1:02d}"
    return cache_dir / symbol / str(day.year) / month_0 / f"{day.day:02d}_{side}.csv.zst"


def load_dukascopy_bidask_minutes(
    cache_dir: Path,
    symbol: str,
    date_from: date,
    date_to: date,
) -> pd.DataFrame:
    """Load 1-minute bid/ask OHLCV bars from the Dukascopy cache.

    Returns a DataFrame indexed by UTC ``DatetimeIndex`` with columns:

    * ``open``, ``high``, ``low``, ``close``, ``volume`` — mid OHLCV
      (mean of bid/ask OHLC, max of high, min of low, summed volume)
    * ``bid_close``, ``ask_close`` — for spread-aware labels and
      microstructure features in :class:`FeatureBuilder`.

    Days where either bid or ask data are missing are skipped silently.
    """
    bid_frames: list[pd.DataFrame] = []
    ask_frames: list[pd.DataFrame] = []
    d = date_from
    while d <= date_to:
        bid = _load_zst_csv(_candle_path(cache_dir, symbol, d, "bid"))
        ask = _load_zst_csv(_candle_path(cache_dir, symbol, d, "ask"))
        if bid is not None and ask is not None and not bid.empty and not ask.empty:
            bid_frames.append(bid)
            ask_frames.append(ask)
        d += timedelta(days=1)

    if not bid_frames:
        raise ValueError(
            f"No bid/ask data for {symbol!r} in {cache_dir} "
            f"between {date_from} and {date_to}."
        )

    bid_df = pd.concat(bid_frames).sort_index()
    ask_df = pd.concat(ask_frames).sort_index()
    # Inner join keeps only timestamps present on BOTH sides — avoids NaNs at
    # the bid/ask boundary that would later collapse mid OHLC.
    joined = bid_df.join(ask_df, how="inner", lsuffix="_bid", rsuffix="_ask")

    out = pd.DataFrame(
        {
            "open": (joined["open_bid"] + joined["open_ask"]) * 0.5,
            "high": np.maximum(joined["high_bid"], joined["high_ask"]),
            "low": np.minimum(joined["low_bid"], joined["low_ask"]),
            "close": (joined["close_bid"] + joined["close_ask"]) * 0.5,
            "volume": joined["volume_bid"] + joined["volume_ask"],
            "bid_close": joined["close_bid"],
            "ask_close": joined["close_ask"],
        },
        index=joined.index,
    )
    return out


# --------------------------------------------------------------- dataset builder


def build_dataset(
    bars: pd.DataFrame,
    horizon: int,
    *,
    feature_config: FeatureConfig | None = None,
    label_neutral_band: float = 0.0,
) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Build aligned ``(X, y, forward_returns)`` for one forward horizon.

    Args:
        bars: 1-minute OHLC(V) bid/ask frame as returned by
            :func:`load_dukascopy_bidask_minutes`.
        horizon: Forward look ``h`` in bars.
        feature_config: Optional :class:`FeatureConfig` override.
        label_neutral_band: Forward-return magnitude below which the
            label is treated as flat (0). ``0.0`` (default) gives pure
            sign labels; set to e.g. ``0.0001`` (1bp) to absorb a rough
            transaction cost.

    Returns:
        ``(X, y_binary, forward_returns)`` where:

        * ``X`` — feature matrix from :class:`FeatureBuilder`
        * ``y_binary`` — Int64 series in ``{0, 1}`` (``1 = up``)
        * ``forward_returns`` — float64 series of mid close-to-close
          forward returns at ``+h`` bars

        All three share the same DatetimeIndex (intersection of feature
        warmup and label tail).
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")

    builder = FeatureBuilder(config=feature_config)
    X = builder.transform(bars)

    label_cfg = LabelConfig(horizon=horizon, neutral_band=label_neutral_band)
    raw_labels = forward_return_labels(bars, label_cfg)

    closes = bars["close"].astype(float)
    fr = closes.shift(-horizon) / closes - 1.0
    fr.name = "forward_return"

    # Align all three on the feature index, then drop rows where the label
    # tail removed forward data.
    aligned_labels = raw_labels.reindex(X.index)
    aligned_fr = fr.reindex(X.index)
    valid = aligned_labels.notna() & aligned_fr.notna()

    X_valid = X.loc[valid]
    fr_valid = aligned_fr.loc[valid].astype(float)
    # Map ternary {-1, 0, 1} sign labels → binary {0, 1} (down/flat=0, up=1).
    raw_int = aligned_labels.loc[valid].astype("int64")
    y_binary = (raw_int > 0).astype("int64")
    y_binary.name = "y"

    return X_valid, y_binary, fr_valid


def build_dataset_directional(
    bars: pd.DataFrame,
    horizon: int,
    *,
    feature_config: FeatureConfig | None = None,
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    """Spread-aware variant of :func:`build_dataset`.

    Builds aligned ``(X, y_binary, fr_long, fr_short)`` where:

    * ``X`` — feature matrix from :class:`FeatureBuilder`.
    * ``y_binary`` — ``Int64`` series in ``{0, 1}``. Derived from
      :func:`forward_return_labels` with ``spread_aware=True`` and
      ``neutral_band=0`` and mapped via ``(raw == 1).astype(int64)`` —
      ``1`` only where the long round-trip is profitable, ``0`` otherwise
      (covers down, flat, and the spread-aware "no clear edge" rows).
    * ``fr_long`` — per-bar **long** round-trip return, i.e.
      ``bid_close[t+h] / ask_close[t] - 1``. Positive when going long is
      profitable.
    * ``fr_short`` — per-bar **short** round-trip return, i.e.
      ``bid_close[t] / ask_close[t+h] - 1``. Positive when going short is
      profitable.

    Args:
        bars: 1-minute bid/ask OHLC frame as returned by
            :func:`load_dukascopy_bidask_minutes`. Must include
            ``bid_close`` and ``ask_close`` columns.
        horizon: Forward look ``h`` in bars.
        feature_config: Optional :class:`FeatureConfig` override.

    Returns:
        ``(X, y_binary, fr_long, fr_short)`` all aligned on the same
        DatetimeIndex (intersection of feature warmup and label tail).

    Raises:
        ValueError: ``horizon < 1`` or missing required columns.
    """
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    missing = [c for c in ("bid_close", "ask_close") if c not in bars.columns]
    if missing:
        raise ValueError(
            f"build_dataset_directional requires columns {missing} in input bars"
        )

    builder = FeatureBuilder(config=feature_config)
    X = builder.transform(bars)

    label_cfg = LabelConfig(horizon=horizon, neutral_band=0.0, spread_aware=True)
    raw_labels = forward_return_labels(bars, label_cfg)

    bid = bars["bid_close"].astype(float)
    ask = bars["ask_close"].astype(float)
    long_fr = bid.shift(-horizon) / ask - 1.0
    short_fr = bid / ask.shift(-horizon) - 1.0
    long_fr.name = "forward_return_long"
    short_fr.name = "forward_return_short"

    aligned_labels = raw_labels.reindex(X.index)
    aligned_long = long_fr.reindex(X.index)
    aligned_short = short_fr.reindex(X.index)
    valid = (
        aligned_labels.notna()
        & aligned_long.notna()
        & aligned_short.notna()
    )

    X_valid = X.loc[valid]
    fr_long_valid = aligned_long.loc[valid].astype(float)
    fr_short_valid = aligned_short.loc[valid].astype(float)
    raw_int = aligned_labels.loc[valid].astype("int64")
    y_binary = (raw_int == 1).astype("int64")
    y_binary.name = "y"

    return X_valid, y_binary, fr_long_valid, fr_short_valid


# --------------------------------------------------------------- run config


@dataclass(frozen=True)
class WalkForwardRunConfig:
    """Configuration for :func:`run_walk_forward`.

    Defaults target the Phase 6 first-pass run: a sliding 1-year
    train window with 3-month test folds, ``embargo`` matching ``purge =
    horizon`` so the gap absorbs both label overlap and short-term feature
    autocorrelation.

    Set ``spread_aware=True`` to switch labels to the ask-to-bid
    round-trip variant and feed direction-aware long/short forward returns
    into :func:`walk_forward_evaluate`. The mid-price ``label_neutral_band``
    is ignored on the spread-aware path — round-trip costs are baked into
    the returns rather than into a flat band.
    """

    symbol: str = "EURUSD"
    date_from: date = date(2018, 1, 1)
    date_to: date = date(2026, 1, 1)
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    train_window_bars: int = 365 * 24 * 60  # ~1 year of 24h bars
    test_window_bars: int = 90 * 24 * 60  # ~3 months
    embargo_factor: int = 1  # embargo = embargo_factor * horizon
    threshold: float = 0.55
    feature_config: FeatureConfig = field(default_factory=FeatureConfig)
    model_config: DirectionClassifierConfig = field(
        default_factory=DirectionClassifierConfig
    )
    label_neutral_band: float = 0.0
    periods_per_year: int = MINUTES_PER_TRADING_YEAR
    spread_aware: bool = False


@dataclass(frozen=True)
class WalkForwardRunResult:
    """Result of :func:`run_walk_forward`.

    Attributes:
        config: The :class:`WalkForwardRunConfig` that produced this run.
        bars: The 1-minute OHLC(V) bid/ask frame loaded from the cache.
        per_horizon_metrics: ``{horizon: per-fold metrics DataFrame}``,
            one entry per requested horizon.
    """

    config: WalkForwardRunConfig
    bars: pd.DataFrame
    per_horizon_metrics: dict[int, pd.DataFrame]


# ------------------------------------------------------------------ run


def run_walk_forward(
    cache_dir: Path,
    config: WalkForwardRunConfig | None = None,
) -> WalkForwardRunResult:
    """Load bars, build features/labels, and run walk-forward evaluation.

    For each horizon in ``config.horizons``:

    1. Build aligned ``(X, y, forward_returns)`` via :func:`build_dataset`.
    2. Configure :class:`WalkForwardSplitter` with
       ``purge = horizon`` and ``embargo = horizon * config.embargo_factor``.
    3. Run :func:`walk_forward_evaluate` with a fresh
       :class:`DirectionClassifier` per fold.

    Args:
        cache_dir: Root of the Dukascopy cache directory.
        config: Optional :class:`WalkForwardRunConfig`. Defaults match
            the Phase 6 first-pass spec.

    Returns:
        :class:`WalkForwardRunResult` with the loaded bars and one
        per-fold DataFrame per horizon.
    """
    cfg = config or WalkForwardRunConfig()

    log.info(
        "Loading %s 1-min bid/ask bars from %s to %s",
        cfg.symbol,
        cfg.date_from,
        cfg.date_to,
    )
    bars = load_dukascopy_bidask_minutes(
        cache_dir, cfg.symbol, cfg.date_from, cfg.date_to
    )
    log.info("Loaded %d bars", len(bars))

    per_horizon: dict[int, pd.DataFrame] = {}
    for horizon in cfg.horizons:
        log.info(
            "Building dataset for horizon=%d (spread_aware=%s)",
            horizon,
            cfg.spread_aware,
        )
        if cfg.spread_aware:
            X, y, fr_long, fr_short = build_dataset_directional(
                bars,
                horizon=horizon,
                feature_config=cfg.feature_config,
            )
        else:
            X, y, fr_long = build_dataset(
                bars,
                horizon=horizon,
                feature_config=cfg.feature_config,
                label_neutral_band=cfg.label_neutral_band,
            )
            fr_short = None

        splitter_config = WalkForwardConfig(
            train_window=cfg.train_window_bars,
            test_window=cfg.test_window_bars,
            embargo=horizon * cfg.embargo_factor,
            purge=horizon,
        )
        splitter = WalkForwardSplitter(splitter_config)
        log.info(
            "horizon=%d: %d folds, X=%s, y=%s",
            horizon,
            splitter.n_splits(X),
            X.shape,
            y.shape,
        )

        def factory() -> DirectionClassifier:
            return DirectionClassifier(config=cfg.model_config)

        metrics = walk_forward_evaluate(
            X,
            y,
            splitter,
            model_factory=factory,
            forward_returns=fr_long,
            forward_returns_short=fr_short,
            threshold=cfg.threshold,
            periods_per_year=cfg.periods_per_year,
        )
        per_horizon[horizon] = metrics
        log.info(
            "horizon=%d complete: mean OOS Sharpe=%.3f, median=%.3f, folds=%d",
            horizon,
            float(metrics["sharpe"].mean()) if not metrics.empty else float("nan"),
            float(metrics["sharpe"].median()) if not metrics.empty else float("nan"),
            len(metrics),
        )

    return WalkForwardRunResult(
        config=cfg,
        bars=bars,
        per_horizon_metrics=per_horizon,
    )
