"""Feature engineering for 1-minute FX bars (Phase 6).

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
from datetime import date
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

#: Lookback (in days) for the daily-realised-vol percentile rank.
#: 60 trading days is ~3 months — enough to span one ECB / FOMC cycle.
DEFAULT_REGIME_DAILY_LOOKBACK: Final[int] = 60

#: Lookback (in days) for the rolling vol-of-vol estimator.
DEFAULT_REGIME_VOLOFVOL_LOOKBACK: Final[int] = 30


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
        include_regime_features: Off by default. When ``True``, emit a
            small set of daily-vol-derived regime context columns —
            ``regime_rv_pct`` (percentile rank of yesterday's realised
            vol within the trailing
            :data:`DEFAULT_REGIME_DAILY_LOOKBACK`-day window) and
            ``regime_volofvol`` (rolling std of daily realised vol over
            :data:`DEFAULT_REGIME_VOLOFVOL_LOOKBACK` days). Both features
            are computed off the *previous* completed day's close-to-close
            log returns and forward-filled onto the minute index, so they
            never use information from ``t`` itself. Designed to give the
            model an explicit signal that the deployment regime differs
            from the training regime — a Sharpe -43 fold showed the model
            overfitting absolute price levels with no regime context.
        include_calendar_features: Off by default. When ``True``, emit
            ``month_sin`` / ``month_cos`` (cyclical month-of-year),
            ``week_of_month`` (1–5), and ``is_first_friday`` (a
            non-stateful proxy for the US Non-Farm Payrolls release day,
            which falls on the first Friday of every month at 13:30 UTC).
        macro_event_dates: Optional set of UTC calendar dates flagged as
            macro-event days (e.g. ECB Governing Council meetings, FOMC
            decisions). When non-empty *and* ``include_calendar_features``
            is ``True``, an ``is_macro_event_day`` column is emitted that
            is ``1.0`` on each listed date and ``0.0`` otherwise. Order
            does not matter; duplicates are collapsed. Defaults to an
            empty tuple — callers are expected to supply a date list when
            they want event-day awareness.
        drop_warmup: If ``True`` (default), drop the leading warmup rows so
            every column in the returned frame is non-NaN. The number of
            dropped rows equals :meth:`FeatureBuilder.warmup`. If ``False``,
            keep the rows with explicit ``NaN`` padding.
    """

    return_windows: tuple[int, ...] = DEFAULT_RETURN_WINDOWS
    moment_windows: tuple[int, ...] = DEFAULT_MOMENT_WINDOWS
    include_time_features: bool = True
    include_microstructure: bool = True
    include_regime_features: bool = False
    include_calendar_features: bool = False
    macro_event_dates: tuple[date, ...] = ()
    drop_warmup: bool = True

    def vectorised_warmup(self) -> int:
        """Bars required before every vectorised feature is non-NaN.

        The regime features are warmed off a *daily* series so they do not
        directly drive the minute-bar warmup; the worst case is the
        rolling-window head-of-day NaN which is forward-filled from the
        first valid daily statistic onwards.
        """
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

        Maximum of the vectorised feature warmup, the longest
        :meth:`tradedesk.marketdata.indicators.Indicator.warmup_periods` in
        the configured indicator stack, and (when
        :attr:`FeatureConfig.include_regime_features` is enabled) the
        trading-day lookback required for the daily regime statistics.

        Daily regime stats are computed on a ``resample("1D")`` of intraday
        log returns with NaN weekend bins dropped, so the lookback is
        denominated in *trading* days (1440 bars each) plus one for the
        no-leak shift; an extra day is added so the first post-warmup bar
        lands strictly inside the first valid forward-filled daily window.
        """
        indicator_warmup = max(
            (ind.warmup_periods() for ind in self.indicators.values()),
            default=0,
        )
        base = max(self.config.vectorised_warmup(), indicator_warmup)
        if self.config.include_regime_features:
            trading_days = (
                max(
                    DEFAULT_REGIME_DAILY_LOOKBACK,
                    DEFAULT_REGIME_VOLOFVOL_LOOKBACK,
                )
                + 2  # +1 for no-leak shift, +1 to land inside the ffill bin
            )
            base = max(base, trading_days * 24 * 60)
        return base

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

        # 6) Regime context (opt-in, off by default).
        if cfg.include_regime_features:
            for col_name, col_series in self._regime_features(close, log_ret_1).items():
                out[col_name] = col_series

        # 7) Calendar dummies (opt-in, off by default).
        if cfg.include_calendar_features:
            assert isinstance(bars.index, pd.DatetimeIndex)  # guarded in _validate
            for col_name, col_series in self._calendar_features(bars.index).items():
                out[col_name] = col_series

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

    def _regime_features(
        self,
        close: pd.Series,
        log_ret_1: pd.Series,
    ) -> dict[str, pd.Series]:
        """Compute regime-context features off a daily-resampled vol series.

        Both columns are computed on the **previous** completed day's data
        and forward-filled onto the minute index — i.e. the value at
        timestamp ``t`` reflects only data available before midnight UTC of
        the day containing ``t``. This avoids leaking intraday information
        into a feature that is supposed to flag "regime drift since the
        training window ended".

        Returns a dict of pandas Series, all aligned to ``close.index``:

        * ``regime_rv_pct`` — percentile rank of yesterday's daily realised
          vol (std of intraday 1-min log returns) inside the trailing
          :data:`DEFAULT_REGIME_DAILY_LOOKBACK`-day window. Values are in
          ``[0.0, 1.0]``; ``NaN`` while the lookback is filling.
        * ``regime_volofvol`` — rolling
          :data:`DEFAULT_REGIME_VOLOFVOL_LOOKBACK`-day std of daily realised
          vol. Captures regime *instability* — high values indicate the
          vol surface itself is moving around (the kind of macro-driven
          turbulence that broke the h=60 fold at ``2025-02-24 →
          2025-04-14`` per the regime-overfitting diagnosis).
        """
        # Daily realised vol = std of within-day 1-min log returns.
        # `log_ret_1` is already shifted-diffed so each value is "return
        # ending at this minute"; resampling by day with `std` aggregates
        # only minutes inside that day.
        daily_rv = log_ret_1.resample("1D").std(ddof=0)
        # Drop empty days (weekends) so the rolling window operates on
        # consecutive trading days, then run the rolling stats and
        # re-broadcast onto the calendar index.
        daily_rv = daily_rv.dropna()
        if daily_rv.empty:
            nan_series = pd.Series(np.nan, index=close.index, dtype=float)
            return {
                "regime_rv_pct": nan_series.copy(),
                "regime_volofvol": nan_series.copy(),
            }

        rv_pct_daily = daily_rv.rolling(
            DEFAULT_REGIME_DAILY_LOOKBACK,
            min_periods=DEFAULT_REGIME_DAILY_LOOKBACK,
        ).rank(pct=True)
        volofvol_daily = daily_rv.rolling(
            DEFAULT_REGIME_VOLOFVOL_LOOKBACK,
            min_periods=DEFAULT_REGIME_VOLOFVOL_LOOKBACK,
        ).std(ddof=0)

        # Shift by one day so the feature for any minute inside day D uses
        # data through end of day D-1 (no peek at intraday day-D vol).
        rv_pct_daily = rv_pct_daily.shift(1)
        volofvol_daily = volofvol_daily.shift(1)

        # Re-broadcast onto the minute index. The daily series are indexed
        # at the day boundary (`yyyy-mm-dd 00:00`); reindexing with
        # ``method="ffill"`` carries each daily value forward across all of
        # that day's minute bars.
        rv_pct_minute = rv_pct_daily.reindex(close.index, method="ffill")
        volofvol_minute = volofvol_daily.reindex(close.index, method="ffill")
        return {
            "regime_rv_pct": rv_pct_minute,
            "regime_volofvol": volofvol_minute,
        }

    def _calendar_features(self, ts: pd.DatetimeIndex) -> dict[str, pd.Series]:
        """Compute calendar dummies. ``ts`` must be a UTC ``DatetimeIndex``.

        Emits the columns documented under
        :attr:`FeatureConfig.include_calendar_features`. NFP day is the
        first Friday of every month — derivable from the timestamp alone,
        no calendar lookup required. ECB / FOMC days require an external
        date list passed via :attr:`FeatureConfig.macro_event_dates`.
        """
        cfg = self.config
        month = np.asarray(ts.month)
        day = np.asarray(ts.day)
        weekday = np.asarray(ts.weekday)
        month_angle = 2.0 * math.pi * (month - 1) / 12.0
        # Week of month — 1 for days 1–7, 2 for 8–14, etc. Capped at 5.
        week_of_month = np.minimum(((day - 1) // 7 + 1).astype(np.int64), 5)
        is_first_friday = ((weekday == 4) & (day <= 7)).astype(float)
        out = {
            "month_sin": pd.Series(np.sin(month_angle), index=ts, dtype=float),
            "month_cos": pd.Series(np.cos(month_angle), index=ts, dtype=float),
            "week_of_month": pd.Series(week_of_month, index=ts, dtype=float),
            "is_first_friday": pd.Series(is_first_friday, index=ts, dtype=float),
        }
        if cfg.macro_event_dates:
            event_set = {pd.Timestamp(d).date() for d in cfg.macro_event_dates}
            event_flag = np.fromiter(
                (1.0 if d.date() in event_set else 0.0 for d in ts),
                dtype=float,
                count=len(ts),
            )
            out["is_macro_event_day"] = pd.Series(event_flag, index=ts, dtype=float)
        return out

    def _run_indicator_stack(self, bars: pd.DataFrame) -> pd.DataFrame:
        """Drive the indicator stack over ``bars`` in chronological order.

        Each indicator is reset before iteration to avoid carrying state
        from prior :meth:`transform` calls. Output columns:

        * Scalar-output indicators emit a single column named after the key.
        * Dict-output indicators emit ``"<key>_<subkey>"`` columns. When the
          subkey equals the key (e.g. ADX's ``adx`` subkey under key
          ``adx``), the bare key is used to avoid the redundant
          ``adx_adx`` column.

        Memory layout: column-major NaN-filled ``np.float64`` buffers are
        allocated lazily on first emission and written in place per bar.
        Earlier revisions accumulated a ``list[dict[str, float]]`` (one dict
        per bar) which OOM'd above ~750 K bars on the 8 GB workspace
        A single :class:`Candle` instance is reused across bars —
        no indicator retains the reference past ``update``.
        """
        indicators = self.indicators
        for ind in indicators.values():
            ind.reset()

        n = len(bars)
        timestamps = bars.index
        opens = bars["open"].astype(float).to_numpy()
        highs = bars["high"].astype(float).to_numpy()
        lows = bars["low"].astype(float).to_numpy()
        closes = bars["close"].astype(float).to_numpy()
        volumes = (
            bars["volume"].astype(float).to_numpy()
            if "volume" in bars.columns
            else np.zeros(n, dtype=float)
        )
        # VWAP parses ``str(candle.timestamp)`` for session reset; pre-render
        # once instead of per-bar to keep the inner loop allocation-light.
        ts_strings = [str(ts) for ts in timestamps]

        candle = Candle(
            timestamp="",
            open=0.0,
            high=0.0,
            low=0.0,
            close=0.0,
            volume=0.0,
        )

        buffers: dict[str, np.ndarray] = {}

        def _store(col: str, idx: int, val: float) -> None:
            buf = buffers.get(col)
            if buf is None:
                buf = np.full(n, np.nan, dtype=np.float64)
                buffers[col] = buf
            buf[idx] = val

        for i in range(n):
            candle.timestamp = ts_strings[i]
            candle.open = float(opens[i])
            candle.high = float(highs[i])
            candle.low = float(lows[i])
            candle.close = float(closes[i])
            candle.volume = float(volumes[i])

            for name, ind in indicators.items():
                value = ind.update(candle)
                if value is None:
                    continue
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if sub_value is None:
                            continue
                        col = name if sub_key == name else f"{name}_{sub_key}"
                        _store(col, i, float(sub_value))
                else:
                    _store(name, i, float(value))

        return pd.DataFrame(buffers, index=timestamps)
