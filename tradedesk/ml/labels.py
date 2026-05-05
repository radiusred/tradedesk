"""Label engineering for 1-minute FX bars (Phase 6 / RAD-896).

Two label families are exposed:

1. **Forward-return binary labels** — :func:`forward_return_labels`.

   For each bar ``t`` look ``h`` bars ahead and label by the sign of the
   forward return:

   .. math::

       y_t = \\operatorname{sign}\\!\\left(\\frac{c_{t+h}}{c_t} - 1\\right)

   With ``spread_aware=True`` the label uses an ask-to-bid round trip:
   the long return is ``bid_close[t+h] / ask_close[t] - 1`` and the short
   return is ``bid_close[t] / ask_close[t+h] - 1``. Both have to clear the
   ``neutral_band`` to flip the label off zero, so realistic transaction
   costs are baked in. With ``spread_aware=False`` (default) the mid
   close-to-close return is used.

   Output is an ``Int8`` series in :math:`\\{-1, 0, 1\\}` aligned to the
   input index, with the trailing ``h`` rows set to NaN (insufficient
   forward data) — pandas promotes the dtype to nullable ``Int8`` so NaN
   sentinels survive without silently turning the whole column to float.

2. **Triple-barrier labels** (López de Prado, *Advances in Financial
   Machine Learning*) — :func:`triple_barrier_labels`.

   For each bar ``t`` the entry mid-close ``c_t`` is bracketed by

   * an upper target ``c_t + mult * ATR_t``
   * a lower stop ``c_t - mult * ATR_t``
   * a vertical barrier ``h`` bars in the future.

   Walking forward bar-by-bar, the **first** barrier touched determines
   the label: ``+1`` upper, ``-1`` lower, vertical barrier labels by the
   sign of the close-to-close return at ``t + h`` (zero inside
   ``vertical_band``). When both upper and lower are touched within the
   *same* bar (intra-bar order unknown) the label is set to ``0`` and the
   row is flagged ``ambiguous`` in ``barrier``. Bars without a valid
   ATR (warmup) and the trailing ``h`` rows are NaN.

A small **class-balance reporter** (:func:`class_balance_report` /
:func:`print_class_balance`) summarises label distributions per
walk-forward fold so we can spot degenerate-class folds before training.

The output format is documented in the docstrings and exercised by the
unit tests under ``tests/ml/test_labels.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import numpy as np
import pandas as pd

from tradedesk.marketdata.indicators import ATR
from tradedesk.types import Candle

#: Required mid OHLC columns for both label families.
REQUIRED_OHLC: Final[tuple[str, ...]] = ("open", "high", "low", "close")

#: Required bid/ask columns when ``spread_aware=True``.
SPREAD_AWARE_COLUMNS: Final[tuple[str, ...]] = ("bid_close", "ask_close")

#: Class labels emitted by both label families.
LABEL_CLASSES: Final[tuple[int, ...]] = (-1, 0, 1)

#: ``barrier`` column values produced by :func:`triple_barrier_labels`.
BarrierKind = Literal["upper", "lower", "vertical", "ambiguous", "warmup"]


# ----------------------------------------------------------------- forward-return


@dataclass(frozen=True)
class LabelConfig:
    """Configuration for :func:`forward_return_labels`.

    Attributes:
        horizon: Forward look ``h`` in bars. Must be ``>= 1``.
        neutral_band: Absolute return threshold below which the label is
            ``0``. Expressed as a simple return (e.g. ``0.0001`` = 1bp).
            Must be ``>= 0``.
        spread_aware: If ``True``, label uses ask-to-bid round-trip
            returns and requires ``bid_close``/``ask_close`` columns; if
            ``False``, mid close-to-close return is used.
    """

    horizon: int = 5
    neutral_band: float = 0.0
    spread_aware: bool = False

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")
        if self.neutral_band < 0:
            raise ValueError("neutral_band must be >= 0")


def forward_return_labels(
    bars: pd.DataFrame,
    config: LabelConfig | None = None,
) -> pd.Series:
    """Compute forward-return binary labels.

    Args:
        bars: DataFrame indexed by a monotonically increasing
            ``DatetimeIndex`` with at least the columns ``open``,
            ``high``, ``low``, ``close``. When ``config.spread_aware`` is
            ``True``, columns ``bid_close`` and ``ask_close`` must also
            be present.
        config: Optional :class:`LabelConfig`; defaults to
            ``LabelConfig()``.

    Returns:
        Nullable ``Int8`` :class:`pandas.Series` aligned to ``bars.index``
        with values in :math:`\\{-1, 0, 1\\}`. The trailing ``horizon``
        rows are ``NaN`` (insufficient forward data).

    Raises:
        ValueError: missing required columns or non-monotonic index.
    """
    cfg = config or LabelConfig()
    _validate_index(bars)
    _require_columns(bars, REQUIRED_OHLC, "forward_return_labels")
    if cfg.spread_aware:
        _require_columns(bars, SPREAD_AWARE_COLUMNS, "forward_return_labels(spread_aware=True)")

    h = cfg.horizon
    band = cfg.neutral_band

    n = len(bars)
    raw = np.zeros(n, dtype=np.int8)

    if cfg.spread_aware:
        ask = bars["ask_close"].astype(float).to_numpy()
        bid = bars["bid_close"].astype(float).to_numpy()
        # Long round-trip (buy at ask_t, sell at bid_{t+h}) and short
        # round-trip (sell at bid_t, buy back at ask_{t+h}) — both
        # expressed as POSITIVE-when-profitable simple returns.
        long_ret = np.full(n, np.nan)
        short_ret = np.full(n, np.nan)
        if n > h:
            long_ret[: n - h] = bid[h:] / ask[: n - h] - 1.0
            short_ret[: n - h] = bid[: n - h] / ask[h:] - 1.0
        up = long_ret > band
        down = short_ret > band
        # Both above band is theoretically impossible while bid <= ask, but
        # numerical noise on tied prices can produce both — pick the larger.
        both = up & down
        raw = np.where(up & ~down, 1, raw)
        raw = np.where(down & ~up, -1, raw)
        if both.any():
            raw = np.where(both & (long_ret >= short_ret), 1, raw)
            raw = np.where(both & (long_ret < short_ret), -1, raw)
    else:
        close = bars["close"].astype(float).to_numpy()
        ret = np.full(n, np.nan)
        if n > h:
            ret[: n - h] = close[h:] / close[: n - h] - 1.0
        raw = np.where(ret > band, 1, raw)
        raw = np.where(ret < -band, -1, raw)

    mask = np.zeros(n, dtype=bool)
    if h > 0:
        mask[-h:] = True  # trailing h rows have no forward data

    array = pd.array(raw.astype(np.int8), dtype="Int8")
    array[mask] = pd.NA
    return pd.Series(array, index=bars.index, name="label")


# ------------------------------------------------------------------ triple-barrier


@dataclass(frozen=True)
class TripleBarrierConfig:
    """Configuration for :func:`triple_barrier_labels`.

    Attributes:
        horizon: Vertical barrier in bars. Must be ``>= 1``.
        atr_period: Wilder ATR lookback used for barrier sizing.
        barrier_mult: Symmetric multiplier on ATR for upper target and
            lower stop. Set ``upper_mult`` / ``lower_mult`` for an
            asymmetric override.
        upper_mult: Optional asymmetric override; defaults to
            ``barrier_mult``.
        lower_mult: Optional asymmetric override; defaults to
            ``barrier_mult``.
        vertical_band: Absolute return threshold below which the
            vertical-barrier label is ``0``. Expressed as a simple
            return.
    """

    horizon: int = 30
    atr_period: int = 14
    barrier_mult: float = 2.0
    upper_mult: float | None = None
    lower_mult: float | None = None
    vertical_band: float = 0.0

    def __post_init__(self) -> None:
        if self.horizon < 1:
            raise ValueError("horizon must be >= 1")
        if self.atr_period < 1:
            raise ValueError("atr_period must be >= 1")
        if self.barrier_mult <= 0:
            raise ValueError("barrier_mult must be > 0")
        if self.upper_mult is not None and self.upper_mult <= 0:
            raise ValueError("upper_mult must be > 0")
        if self.lower_mult is not None and self.lower_mult <= 0:
            raise ValueError("lower_mult must be > 0")
        if self.vertical_band < 0:
            raise ValueError("vertical_band must be >= 0")

    @property
    def effective_upper_mult(self) -> float:
        return self.upper_mult if self.upper_mult is not None else self.barrier_mult

    @property
    def effective_lower_mult(self) -> float:
        return self.lower_mult if self.lower_mult is not None else self.barrier_mult


def triple_barrier_labels(
    bars: pd.DataFrame,
    config: TripleBarrierConfig | None = None,
) -> pd.DataFrame:
    """Compute triple-barrier labels.

    The first barrier touched within ``[t+1, t+horizon]`` decides the
    label:

    * Upper target hit first → ``+1``
    * Lower stop hit first → ``-1``
    * Neither touched → vertical-barrier label by sign of
      ``close[t+h] / close[t] - 1`` (zero inside ``vertical_band``)
    * Both touched within the **same bar** → ``0`` with
      ``barrier='ambiguous'`` (intra-bar order unknown).

    Args:
        bars: DataFrame indexed by a monotonically increasing
            ``DatetimeIndex`` with columns ``open``, ``high``, ``low``,
            ``close``.
        config: Optional :class:`TripleBarrierConfig`; defaults to
            ``TripleBarrierConfig()``.

    Returns:
        DataFrame indexed by ``bars.index`` with columns:

        * ``label`` — nullable ``Int8`` in :math:`\\{-1, 0, 1\\}`. NaN
          during ATR warmup and on the trailing ``horizon`` rows.
        * ``exit_offset`` — nullable ``Int32`` number of bars from
          entry to exit (``1..horizon``). NaN where ``label`` is NaN.
        * ``barrier`` — string in
          ``{"upper", "lower", "vertical", "ambiguous", "warmup"}``.
          ``"warmup"`` covers ATR warmup *and* the trailing horizon
          rows where forward data is insufficient.

    Raises:
        ValueError: missing required columns or non-monotonic index.
    """
    cfg = config or TripleBarrierConfig()
    _validate_index(bars)
    _require_columns(bars, REQUIRED_OHLC, "triple_barrier_labels")

    n = len(bars)
    closes = bars["close"].astype(float).to_numpy()
    highs = bars["high"].astype(float).to_numpy()
    lows = bars["low"].astype(float).to_numpy()
    opens = bars["open"].astype(float).to_numpy()
    volumes = (
        bars["volume"].astype(float).to_numpy()
        if "volume" in bars.columns
        else np.zeros(n, dtype=float)
    )
    timestamps = bars.index

    atr = ATR(period=cfg.atr_period)
    atr_values = np.full(n, np.nan)
    for i in range(n):
        candle = Candle(
            timestamp=str(timestamps[i]),
            open=float(opens[i]),
            high=float(highs[i]),
            low=float(lows[i]),
            close=float(closes[i]),
            volume=float(volumes[i]),
        )
        v = atr.update(candle)
        if v is not None:
            atr_values[i] = float(v)

    h = cfg.horizon
    upper_mult = cfg.effective_upper_mult
    lower_mult = cfg.effective_lower_mult
    band = cfg.vertical_band

    labels = np.full(n, np.iinfo(np.int8).min, dtype=np.int8)
    label_valid = np.zeros(n, dtype=bool)
    exit_offset = np.full(n, -1, dtype=np.int32)
    exit_valid = np.zeros(n, dtype=bool)
    barrier = np.full(n, "warmup", dtype=object)

    last_t = n - h - 1
    for t in range(last_t + 1):
        a = atr_values[t]
        if not np.isfinite(a):
            continue
        upper = closes[t] + a * upper_mult
        lower = closes[t] - a * lower_mult
        upper_touch = -1
        lower_touch = -1
        for j in range(1, h + 1):
            idx = t + j
            hi = highs[idx]
            lo = lows[idx]
            if upper_touch == -1 and hi >= upper:
                upper_touch = j
            if lower_touch == -1 and lo <= lower:
                lower_touch = j
            if upper_touch != -1 and lower_touch != -1:
                break

        if upper_touch == -1 and lower_touch == -1:
            net = closes[t + h] / closes[t] - 1.0
            if net > band:
                lbl = 1
            elif net < -band:
                lbl = -1
            else:
                lbl = 0
            labels[t] = lbl
            label_valid[t] = True
            exit_offset[t] = h
            exit_valid[t] = True
            barrier[t] = "vertical"
        elif upper_touch != -1 and (lower_touch == -1 or upper_touch < lower_touch):
            labels[t] = 1
            label_valid[t] = True
            exit_offset[t] = upper_touch
            exit_valid[t] = True
            barrier[t] = "upper"
        elif lower_touch != -1 and (upper_touch == -1 or lower_touch < upper_touch):
            labels[t] = -1
            label_valid[t] = True
            exit_offset[t] = lower_touch
            exit_valid[t] = True
            barrier[t] = "lower"
        else:
            # Same bar — intra-bar order unknown.
            labels[t] = 0
            label_valid[t] = True
            exit_offset[t] = upper_touch  # == lower_touch
            exit_valid[t] = True
            barrier[t] = "ambiguous"

    label_series = pd.array(labels, dtype="Int8")
    label_series[~label_valid] = pd.NA
    exit_series = pd.array(exit_offset, dtype="Int32")
    exit_series[~exit_valid] = pd.NA

    return pd.DataFrame(
        {
            "label": label_series,
            "exit_offset": exit_series,
            "barrier": barrier,
        },
        index=bars.index,
    )


# ------------------------------------------------------------------ class balance


def class_balance_report(
    folds: Mapping[str, pd.Series],
    classes: Sequence[int] = LABEL_CLASSES,
) -> pd.DataFrame:
    """Summarise class balance per fold.

    Args:
        folds: Mapping from fold name to label series. ``NaN`` entries
            are dropped before counting.
        classes: Class labels to report. Defaults to ``(-1, 0, 1)``.

    Returns:
        DataFrame indexed by fold name with columns:

        * ``n`` — non-NaN sample count
        * ``count_<c>`` — count for class ``c``
        * ``prop_<c>`` — proportion (``count_<c> / n``); ``0.0`` if
          ``n == 0``

        Column order is stable: ``n``, then ``count_*``, then ``prop_*``
        in the order of ``classes``.
    """
    rows = []
    for name, y in folds.items():
        y_clean = pd.Series(y).dropna()
        n = int(len(y_clean))
        counts: dict[int, int] = {c: int((y_clean == c).sum()) for c in classes}
        row: dict[str, float | int | str] = {"fold": name, "n": n}
        for c in classes:
            row[f"count_{c}"] = counts[c]
        for c in classes:
            row[f"prop_{c}"] = (counts[c] / n) if n else 0.0
        rows.append(row)

    columns = ["fold", "n"]
    columns += [f"count_{c}" for c in classes]
    columns += [f"prop_{c}" for c in classes]
    return pd.DataFrame(rows, columns=columns).set_index("fold")


def print_class_balance(
    folds: Mapping[str, pd.Series],
    classes: Sequence[int] = LABEL_CLASSES,
) -> pd.DataFrame:
    """Print and return :func:`class_balance_report`.

    The printed table uses :meth:`pandas.DataFrame.to_string` so it
    survives non-rich consoles. The same DataFrame is returned for
    programmatic use.
    """
    report = class_balance_report(folds, classes=classes)
    print(report.to_string(float_format=lambda x: f"{x:.4f}"))
    return report


# ----------------------------------------------------------------------- helpers


def _validate_index(bars: pd.DataFrame) -> None:
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("labels require a DatetimeIndex on the input bars")
    if not bars.index.is_monotonic_increasing:
        raise ValueError("labels require a monotonically increasing index")


def _require_columns(bars: pd.DataFrame, columns: Sequence[str], where: str) -> None:
    missing = [c for c in columns if c not in bars.columns]
    if missing:
        raise ValueError(f"{where} requires columns {missing} in input bars")
