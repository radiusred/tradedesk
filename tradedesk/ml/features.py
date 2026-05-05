"""Feature engineering for 1-minute FX bars (Phase 6 / RAD-896).

This module turns a 1-minute bid/ask OHLCV ``DataFrame`` plus an *indicator
stack* (instances of :class:`tradedesk.marketdata.indicators.Indicator`) into
an aligned feature ``DataFrame`` suitable for an XGBoost direction
classifier.

**No-look-ahead is the load-bearing invariant.** Every feature value at
index ``t`` is computed exclusively from bar data up to and including ``t``:

* Vectorised helpers use ``shift``/``rolling``/``ewm`` only on data ≤ t.
* Indicator outputs are produced by the same streaming
  :class:`~tradedesk.marketdata.indicators.Indicator` classes used in live
  trading, fed bars in chronological order — each ``update(candle_t)`` call
  observes only bars ``≤ t``.
* Forward-return labels live in :mod:`tradedesk.ml.labels`; the feature
  builder never peeks at ``t + h``.

The expected input ``DataFrame`` has a monotonically increasing
``DatetimeIndex`` (UTC) and at least the columns ``open``, ``high``,
``low``, ``close``. Optional columns ``bid_close``, ``ask_close`` and
``volume`` enable extra microstructure / volume features when present.

Typical usage::

    from tradedesk.ml import FeatureBuilder, FeatureConfig
    builder = FeatureBuilder()
    X = builder.transform(bars)

Pass a custom ``indicators`` stack to extend or shrink the indicator-derived
feature set::

    from tradedesk.marketdata.indicators import RSI, ATR
    builder = FeatureBuilder(indicators={"rsi": RSI(period=7), "atr": ATR()})
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd

from tradedesk.marketdata.indicators import (
    ADX,
    ATR,
    CCI,
    EMA,
    MACD,
    MFI,
    OBV,
    RSI,
    SMA,
    VWAP,
    BollingerBands,
    Indicator,
    KeltnerChannel,
    Stochastic,
    WilliamsR,
)
from tradedesk.types import Candle

#: Required input columns. Absence raises :class:`ValueError` early.
REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close")

#: Default rolling windows (in 1-min bars) for lagged log-return features.
#: Sub-hour, hour, multi-hour, and intraday horizons.
DEFAULT_RETURN_WINDOWS: Final[tuple[int, ...]] = (1, 5, 15, 60, 240)

#: Default rolling windows for realised volatility, skew, and kurt.
DEFAULT_MOMENT_WINDOWS: Final[tuple[int, ...]] = (15, 60, 240)


def default_indicator_stack() -> dict[str, Indicator]:
    """Return a fresh copy of the default 14-indicator stack.

    Each call returns *new* indicator instances so the stack can be reused
    across multiple :class:`FeatureBuilder` runs without state bleed.
    """
    return {
        "adx": ADX(period=14),
        "atr": ATR(period=14),
        "bb": BollingerBands(period=20, k=2.0),
        "cci": CCI(period=20),
        "ema": EMA(period=20),
        "kc": KeltnerChannel(period=20, mult=1.5),
        "macd": MACD(fast=12, slow=26, signal=9),
        "mfi": MFI(period=14),
        "obv": OBV(),
        "rsi": RSI(period=14),
        "sma": SMA(period=20),
        "stoch": Stochastic(k_period=14, d_period=3),
        "vwap": VWAP(),
        "williams_r": WilliamsR(period=14),
    }


@dataclass(frozen=True)
class FeatureConfig:
    """Configuration for :class:`FeatureBuilder`.

    Attributes:
        return_windows: Lookbacks (in 1-min bars) for lagged log-return
            features. Emits one ``log_ret_W`` column per window.
        moment_windows: Window sizes for rolling volatility (std of 1-min
            log returns), skew, and kurt. Emits ``vol_W``, ``skew_W``,
            ``kurt_W`` per window.
        include_time_features: Emit cyclical time-of-day (sin/cos of
            minute-of-day) and an integer weekday feature.
        include_microstructure: Emit body/range/wick ratios; emit
            ``spread`` / ``spread_rel`` when ``bid_close`` and
            ``ask_close`` columns are present.
        drop_warmup: If ``True`` (default), drop the leading warmup rows so
            every column in the returned frame is non-NaN. The number of
            dropped rows equals :meth:`FeatureBuilder.warmup`. If ``False``,
            keep the rows with explicit ``NaN`` padding.
    """

    return_windows: tuple[int, ...] = DEFAULT_RETURN_WINDOWS
    moment_windows: tuple[int, ...] = DEFAULT_MOMENT_WINDOWS
    include_time_features: bool = True
    include_microstructure: bool = True
    drop_warmup: bool = True

    def vectorised_warmup(self) -> int:
        """Bars required before every vectorised feature is non-NaN."""
        return max(
            max(self.return_windows, default=1),
            max(self.moment_windows, default=1),
        )


class FeatureBuilder:
    """Build a feature matrix from a 1-minute OHLC(V) bid/ask ``DataFrame``.

    The output ``DataFrame`` shares the input's ``DatetimeIndex`` (less the
    leading warmup rows when :attr:`FeatureConfig.drop_warmup` is True) and
    carries one column per emitted feature. Column order is stable across
    runs so feature-importance reports can be diffed across walk-forward
    folds.
    """

    def __init__(
        self,
        config: FeatureConfig | None = None,
        indicators: Mapping[str, Indicator] | None = None,
    ) -> None:
        self.config = config or FeatureConfig()
        if indicators is None:
            self.indicators: dict[str, Indicator] = default_indicator_stack()
        else:
            self.indicators = dict(indicators)

    # ------------------------------------------------------------------ public

    def warmup(self) -> int:
        """Bars required before every feature column is non-NaN.

        Maximum of the vectorised feature warmup and the longest
        :meth:`tradedesk.marketdata.indicators.Indicator.warmup_periods` in
        the configured indicator stack.
        """
        indicator_warmup = max(
            (ind.warmup_periods() for ind in self.indicators.values()),
            default=0,
        )
        return max(self.config.vectorised_warmup(), indicator_warmup)

    def transform(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Compute the feature matrix.

        Args:
            bars: 1-minute OHLC(V) frame indexed by ``DatetimeIndex``.

        Returns:
            DataFrame indexed by the same timestamps (less warmup rows when
            ``drop_warmup``) with one column per feature.

        Raises:
            ValueError: Missing required columns or non-monotonic index.
        """
        self._validate(bars)
        cfg = self.config

        out: dict[str, pd.Series] = {}

        close = bars["close"].astype(float)
        log_close = pd.Series(np.log(close.to_numpy()), index=close.index)
        log_ret_1 = log_close.diff()

        # 1) Lagged log returns over a fan of horizons.
        for w in cfg.return_windows:
            out[f"log_ret_{w}"] = log_close.diff(w)

        # 2) Rolling realised vol / skew / kurt of 1-min log returns.
        for w in cfg.moment_windows:
            window = log_ret_1.rolling(w)
            out[f"vol_{w}"] = window.std(ddof=0)
            out[f"skew_{w}"] = window.skew()
            out[f"kurt_{w}"] = window.kurt()

        # 3) Time-of-day (cyclical) + weekday — captures session structure.
        if cfg.include_time_features:
            ts = bars.index
            assert isinstance(ts, pd.DatetimeIndex)  # guarded in _validate
            minute_of_day = ts.hour.values * 60 + ts.minute.values
            angle = 2.0 * math.pi * minute_of_day / (24 * 60)
            out["tod_sin"] = pd.Series(np.sin(angle), index=ts)
            out["tod_cos"] = pd.Series(np.cos(angle), index=ts)
            out["weekday"] = pd.Series(ts.weekday, index=ts).astype(float)

        # 4) Indicator stack — driven by the actual streaming indicator
        # classes so live and backtest features are bit-identical.
        indicator_frame = self._run_indicator_stack(bars)
        for col in indicator_frame.columns:
            out[col] = indicator_frame[col]

        # 5) Microstructure: body/range/wick ratios + bid/ask spread.
        if cfg.include_microstructure:
            high = bars["high"].astype(float)
            low = bars["low"].astype(float)
            open_ = bars["open"].astype(float)
            rng = (high - low).replace(0.0, np.nan)
            body = (close - open_).abs()
            out["body_range_ratio"] = body / rng
            out["upper_wick_ratio"] = (high - pd.concat([open_, close], axis=1).max(axis=1)) / rng
            out["lower_wick_ratio"] = (pd.concat([open_, close], axis=1).min(axis=1) - low) / rng
            if {"bid_close", "ask_close"}.issubset(bars.columns):
                spread = bars["ask_close"].astype(float) - bars["bid_close"].astype(float)
                out["spread"] = spread
                out["spread_rel"] = spread / close

        features = pd.DataFrame(out, index=bars.index)
        if cfg.drop_warmup:
            features = features.iloc[self.warmup() :]
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

    def _run_indicator_stack(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Drive the indicator stack over ``bars`` in chronological order.

        Each indicator is reset before iteration to avoid carrying state
        from prior :meth:`transform` calls. Output columns:

        * Scalar-output indicators emit a single column named after the key.
        * Dict-output indicators emit ``"<key>_<subkey>"`` columns. When the
          subkey equals the key (e.g. ADX's ``adx`` subkey under key
          ``adx``), the bare key is used to avoid the redundant
          ``adx_adx`` column.
        """
        indicators = self.indicators
        for ind in indicators.values():
            ind.reset()

        timestamps = bars.index
        opens = bars["open"].astype(float).to_numpy()
        highs = bars["high"].astype(float).to_numpy()
        lows = bars["low"].astype(float).to_numpy()
        closes = bars["close"].astype(float).to_numpy()
        volumes = (
            bars["volume"].astype(float).to_numpy()
            if "volume" in bars.columns
            else np.zeros(len(bars), dtype=float)
        )

        rows: list[dict[str, float]] = []
        for i, ts in enumerate(timestamps):
            candle = Candle(
                timestamp=str(ts),
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=float(volumes[i]),
            )
            row: dict[str, float] = {}
            for name, ind in indicators.items():
                value = ind.update(candle)
                if value is None:
                    continue
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if sub_value is None:
                            continue
                        col = name if sub_key == name else f"{name}_{sub_key}"
                        row[col] = float(sub_value)
                else:
                    row[name] = float(value)
            rows.append(row)

        return pd.DataFrame(rows, index=timestamps).astype(float)
