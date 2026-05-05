"""Vectorised feature engineering for 1-minute FX bars.

Built for the Phase 6 XGBoost direction classifier (RAD-896). The builder is
deliberately pandas-first and entirely vectorised — no per-bar Python loops —
so it scales to the 8-year Dukascopy walk-forward window without becoming the
bottleneck.

**Strict no-look-ahead.** Every column at index ``t`` depends only on bar data
up to and including ``t``. Labels (forward returns) live in
:mod:`tradedesk.ml.labels` and are appended downstream of the feature matrix;
the splitter in :mod:`tradedesk.ml.cv` is responsible for the embargo/purge
that guards against label leakage when the horizon overlaps a fold boundary.

The expected input is a pandas ``DataFrame`` indexed by a monotonically
increasing ``DatetimeIndex`` (UTC) with at least the columns ``open``,
``high``, ``low``, ``close``. Optional columns ``bid_close``, ``ask_close``,
and ``volume`` enable extra feature families when present.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

import numpy as np
import pandas as pd

#: Required input columns. Absence raises :class:`ValueError` early.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")

#: Default rolling windows (in 1-min bars) used for return / volatility / moment
#: features. Chosen to span sub-hour, hour, and intraday horizons without being
#: redundant with each other.
DEFAULT_WINDOWS: Final[tuple[int, ...]] = (1, 5, 15, 60, 240)

#: Default lookback windows for momentum / ATR / RSI / EMA features.
DEFAULT_INDICATOR_WINDOWS: Final[tuple[int, ...]] = (14, 30, 60)


@dataclass(frozen=True)
class FeatureConfig:
    """Feature builder configuration.

    Attributes:
        return_windows: Lookbacks (in bars) for lagged log-return features.
        vol_windows: Rolling-window sizes for realised volatility / skew / kurt.
        momentum_windows: Lookbacks for cumulative-return momentum features.
        indicator_windows: Periods for ATR, RSI, and EMA-distance indicators.
        bb_window: Bollinger-band period (close mean +/- ``bb_std`` rolling std).
        bb_std: Bollinger-band standard-deviation multiplier.
        macd_fast: MACD fast EMA period.
        macd_slow: MACD slow EMA period.
        macd_signal: MACD signal-line EMA period.
        include_time_features: Emit time-of-day (sin/cos) and weekday features.
        include_microstructure: Emit body/range/wick ratios + bid/ask spread
            features when bid/ask columns are available.
        drop_warmup: If True, drop the leading rows where any feature is NaN
            (the longest indicator window). If False, keep NaNs and let the
            model / pipeline handle them.
    """

    return_windows: tuple[int, ...] = DEFAULT_WINDOWS
    vol_windows: tuple[int, ...] = (15, 60, 240)
    momentum_windows: tuple[int, ...] = (5, 15, 60)
    indicator_windows: tuple[int, ...] = DEFAULT_INDICATOR_WINDOWS
    bb_window: int = 20
    bb_std: float = 2.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    include_time_features: bool = True
    include_microstructure: bool = True
    drop_warmup: bool = True
    extra_columns: tuple[str, ...] = field(default_factory=tuple)

    def warmup(self) -> int:
        """Bars required before *every* feature is non-NaN."""
        candidates = [
            max(self.return_windows, default=1),
            max(self.vol_windows, default=1),
            max(self.momentum_windows, default=1),
            max(self.indicator_windows, default=1),
            self.bb_window,
            self.macd_slow + self.macd_signal,
        ]
        return max(candidates)


class FeatureBuilder:
    """Build a feature matrix from a 1-minute OHLC(V) bid/ask DataFrame.

    Usage:

        >>> builder = FeatureBuilder()
        >>> X = builder.transform(bars)

    The output is a ``DataFrame`` with the same ``DatetimeIndex`` as the input
    (less the warmup prefix when :attr:`FeatureConfig.drop_warmup` is True) and
    one column per emitted feature. Column names are stable so downstream
    feature-importance reports survive cross-fold rebuilds.
    """

    def __init__(self, config: FeatureConfig | None = None) -> None:
        self.config = config or FeatureConfig()

    # ------------------------------------------------------------------ public

    def transform(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Compute the feature matrix.

        Raises:
            ValueError: If required columns are missing or the index is not
                monotonically increasing.
        """
        self._validate(bars)
        cfg = self.config

        close = bars["close"]
        log_close = np.log(close)
        log_ret_1 = log_close.diff()

        out: dict[str, pd.Series] = {}

        # Lagged log returns over a fan of horizons.
        for w in cfg.return_windows:
            out[f"log_ret_{w}"] = log_close.diff(w)

        # Rolling realised volatility, skew, kurt of 1-min log returns.
        for w in cfg.vol_windows:
            window = log_ret_1.rolling(w)
            out[f"vol_{w}"] = window.std(ddof=0)
            out[f"skew_{w}"] = window.skew()
            out[f"kurt_{w}"] = window.kurt()

        # Cumulative momentum (sum of log returns over a window).
        for w in cfg.momentum_windows:
            out[f"mom_{w}"] = log_ret_1.rolling(w).sum()

        # ATR family (true range / rolling mean / close-to-atr ratio).
        tr = self._true_range(bars)
        for w in cfg.indicator_windows:
            atr = tr.rolling(w).mean()
            out[f"atr_{w}"] = atr
            out[f"close_atr_ratio_{w}"] = (close - close.rolling(w).mean()) / atr.replace(0, np.nan)

        # Wilder RSI on close.
        for w in cfg.indicator_windows:
            out[f"rsi_{w}"] = self._rsi(close, w)

        # EMA distance + slope for fast trend bias.
        for w in cfg.indicator_windows:
            ema = close.ewm(span=w, adjust=False, min_periods=w).mean()
            out[f"ema_dist_{w}"] = (close - ema) / close
            out[f"ema_slope_{w}"] = ema.diff() / close

        # MACD (fast EMA - slow EMA), signal line, histogram.
        ema_fast = close.ewm(span=cfg.macd_fast, adjust=False, min_periods=cfg.macd_fast).mean()
        ema_slow = close.ewm(span=cfg.macd_slow, adjust=False, min_periods=cfg.macd_slow).mean()
        macd = ema_fast - ema_slow
        signal = macd.ewm(span=cfg.macd_signal, adjust=False, min_periods=cfg.macd_signal).mean()
        out["macd"] = macd / close
        out["macd_signal"] = signal / close
        out["macd_hist"] = (macd - signal) / close

        # Bollinger position (z-score of close vs rolling mean of close).
        bb_mean = close.rolling(cfg.bb_window).mean()
        bb_std = close.rolling(cfg.bb_window).std(ddof=0)
        out[f"bb_z_{cfg.bb_window}"] = (close - bb_mean) / bb_std.replace(0, np.nan)

        # Time-of-day (cyclical) + weekday — captures session structure.
        if cfg.include_time_features:
            ts = bars.index
            if not isinstance(ts, pd.DatetimeIndex):  # pragma: no cover - validated above
                raise ValueError("Index must be a DatetimeIndex for time features")
            minute_of_day = ts.hour * 60 + ts.minute
            out["tod_sin"] = pd.Series(np.sin(2 * math.pi * minute_of_day / (24 * 60)), index=ts)
            out["tod_cos"] = pd.Series(np.cos(2 * math.pi * minute_of_day / (24 * 60)), index=ts)
            out["weekday"] = pd.Series(ts.weekday, index=ts).astype(float)

        # Microstructure: body/range/wick ratios + bid/ask spread.
        if cfg.include_microstructure:
            rng = (bars["high"] - bars["low"]).replace(0, np.nan)
            body = (bars["close"] - bars["open"]).abs()
            out["body_range_ratio"] = body / rng
            out["upper_wick_ratio"] = (bars["high"] - bars[["open", "close"]].max(axis=1)) / rng
            out["lower_wick_ratio"] = (bars[["open", "close"]].min(axis=1) - bars["low"]) / rng
            if {"bid_close", "ask_close"}.issubset(bars.columns):
                spread = bars["ask_close"] - bars["bid_close"]
                out["spread"] = spread
                out["spread_rel"] = spread / close

        # Volume features when present (Dukascopy 1-min bars carry tick volume).
        if "volume" in bars.columns:
            vol = bars["volume"].astype(float)
            out["volume"] = vol
            out["volume_log1p"] = pd.Series(np.log1p(vol.to_numpy()), index=vol.index)
            for w in cfg.indicator_windows:
                vol_mean = vol.rolling(w).mean()
                out[f"volume_z_{w}"] = (vol - vol_mean) / vol.rolling(w).std(ddof=0).replace(
                    0, np.nan
                )

        for col in cfg.extra_columns:
            if col in bars.columns:
                out[col] = bars[col].astype(float)

        features = pd.DataFrame(out, index=bars.index)
        if cfg.drop_warmup:
            features = features.iloc[cfg.warmup() :]
        return features

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _validate(bars: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in bars.columns]
        if missing:
            raise ValueError(f"FeatureBuilder requires columns {missing} in input bars")
        if not isinstance(bars.index, pd.DatetimeIndex):
            raise ValueError("FeatureBuilder requires a DatetimeIndex on the input bars")
        if not bars.index.is_monotonic_increasing:
            raise ValueError("FeatureBuilder requires a monotonically increasing index")

    @staticmethod
    def _true_range(bars: pd.DataFrame) -> pd.Series:
        prev_close = bars["close"].shift(1)
        ranges = pd.concat(
            [
                bars["high"] - bars["low"],
                (bars["high"] - prev_close).abs(),
                (bars["low"] - prev_close).abs(),
            ],
            axis=1,
        )
        return ranges.max(axis=1)

    @staticmethod
    def _rsi(close: pd.Series, period: int) -> pd.Series:
        delta = close.diff()
        gains = delta.clip(lower=0.0)
        losses = (-delta).clip(lower=0.0)
        # Wilder smoothing (EMA with alpha = 1/period).
        avg_gain = gains.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        avg_loss = losses.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - 100 / (1 + rs)
