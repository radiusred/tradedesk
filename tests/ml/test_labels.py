"""Unit tests for tradedesk.ml.labels (Phase 6)."""

from __future__ import annotations

import pandas as pd
import pytest

from tradedesk.ml.labels import (
    LabelConfig,
    TripleBarrierConfig,
    class_balance_report,
    forward_return_labels,
    print_class_balance,
    triple_barrier_labels,
)


def _flat_ohlc(closes: list[float]) -> pd.DataFrame:
    """Build an OHLC frame where high = low = open = close (no intra-bar range)."""
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes,
            "low": closes,
            "close": closes,
        },
        index=idx,
    )


# ---------------------------------------------------------------- forward returns


def test_forward_return_labels_known_up_down_flat_sequence():
    # Closes: monotone up, monotone down, then flat — h=1, no neutral band
    closes = [
        100.0,
        101.0,
        102.0,
        103.0,  # up: 3 forward-up labels then transition
        102.0,
        101.0,
        100.0,  # down: 3 forward-down labels then transition
        100.0,
        100.0,
        100.0,  # flat
    ]
    bars = _flat_ohlc(closes)
    config = LabelConfig(horizon=1, neutral_band=0.0, spread_aware=False)

    labels = forward_return_labels(bars, config)

    # Series shape and dtype
    assert len(labels) == len(bars)
    assert str(labels.dtype) == "Int8"

    # close[t+1] / close[t] - 1:
    # 100→101 +, 101→102 +, 102→103 +, 103→102 -, 102→101 -, 101→100 -,
    # 100→100 0, 100→100 0, 100→100 0, NaN (tail h=1)
    expected = [1, 1, 1, -1, -1, -1, 0, 0, 0, pd.NA]

    # Compare element-wise (NA != NA in pandas, so check separately)
    for i, exp in enumerate(expected):
        if exp is pd.NA:
            assert labels.iloc[i] is pd.NA, f"row {i}: expected NA got {labels.iloc[i]}"
        else:
            assert labels.iloc[i] == exp, f"row {i}: expected {exp} got {labels.iloc[i]}"


def test_forward_return_labels_neutral_band_zeros_small_moves():
    # +0.5%, +0.05%, -0.05%, -0.5%, exactly +0.1% (band) → small ones zeroed
    closes = [100.0, 100.5, 100.55, 100.50, 100.0, 100.1]
    bars = _flat_ohlc(closes)
    # band = 0.001 (10bp). Forward returns:
    # 100→100.5 = +0.005 → 1
    # 100.5→100.55 = +0.0005 → 0 (within band)
    # 100.55→100.50 = -0.000497 → 0 (within band)
    # 100.50→100.0 = -0.00498 → -1
    # 100.0→100.1 = +0.001 → 0 (NOT > band, only equal)
    # last → NaN
    config = LabelConfig(horizon=1, neutral_band=0.001)

    labels = forward_return_labels(bars, config)

    expected = [1, 0, 0, -1, 0, pd.NA]
    for i, exp in enumerate(expected):
        if exp is pd.NA:
            assert labels.iloc[i] is pd.NA
        else:
            assert labels.iloc[i] == exp, f"row {i}: expected {exp} got {labels.iloc[i]}"


def test_forward_return_labels_horizon_h_marks_h_tail_nan():
    closes = list(range(100, 120))  # 20 bars, +1 each
    bars = _flat_ohlc([float(c) for c in closes])
    h = 5

    labels = forward_return_labels(bars, LabelConfig(horizon=h))

    # First 15 rows: forward return is +5/c_t > 0 → 1
    for i in range(15):
        assert labels.iloc[i] == 1, f"row {i}"
    # Last 5 rows: NaN (h tail)
    for i in range(15, 20):
        assert labels.iloc[i] is pd.NA, f"row {i}"


def test_forward_return_labels_spread_aware_uses_ask_to_bid_round_trip():
    # 5-bar series. Mid moves +1 then bid/ask spread of 0.5 makes the
    # round-trip a *loss* — spread_aware=True should suppress the +1 label.
    idx = pd.date_range("2026-01-01", periods=5, freq="1min", tz="UTC")
    bars = pd.DataFrame(
        {
            "open": [100.0, 100.2, 100.4, 100.6, 100.8],
            "high": [100.0, 100.2, 100.4, 100.6, 100.8],
            "low": [100.0, 100.2, 100.4, 100.6, 100.8],
            "close": [100.0, 100.2, 100.4, 100.6, 100.8],
            # bid/ask spread of 0.50 — mid drifts +0.20/bar but you buy ask
            # 100.25 and exit bid 100.55 only nets +0.003, while drift over
            # 1 bar is +0.20/100 = +0.002. With h=1 the round-trip return is
            # bid_{t+1}/ask_t - 1 = 99.95/100.25 - 1 ≈ -0.003 → not +1.
            "bid_close": [99.75, 99.95, 100.15, 100.35, 100.55],
            "ask_close": [100.25, 100.45, 100.65, 100.85, 101.05],
        },
        index=idx,
    )

    # With spread_aware=False, mid forward returns are +0.002 each → all 1s.
    mid_labels = forward_return_labels(bars, LabelConfig(horizon=1, spread_aware=False))
    for i in range(4):
        assert mid_labels.iloc[i] == 1
    assert mid_labels.iloc[-1] is pd.NA

    # With spread_aware=True, both long and short round-trips are negative
    # → labels are all 0.
    sa_labels = forward_return_labels(bars, LabelConfig(horizon=1, spread_aware=True))
    for i in range(4):
        assert sa_labels.iloc[i] == 0, f"row {i}: expected 0 got {sa_labels.iloc[i]}"
    assert sa_labels.iloc[-1] is pd.NA


def test_forward_return_labels_spread_aware_requires_bid_ask_columns():
    bars = _flat_ohlc([100.0, 101.0, 102.0])
    with pytest.raises(ValueError, match="bid_close"):
        forward_return_labels(bars, LabelConfig(horizon=1, spread_aware=True))


def test_label_config_validation():
    with pytest.raises(ValueError, match="horizon"):
        LabelConfig(horizon=0)
    with pytest.raises(ValueError, match="neutral_band"):
        LabelConfig(neutral_band=-0.001)


def test_forward_return_labels_n_equals_horizon_marks_every_row_nan():
    """``n == horizon`` is the degenerate boundary: every row is part of the
    h-tail and has no forward data. Output must be all-NA, not raise."""
    bars = _flat_ohlc([100.0, 101.0, 102.0, 103.0, 104.0])
    labels = forward_return_labels(bars, LabelConfig(horizon=5))
    assert len(labels) == 5
    assert all(labels.iloc[i] is pd.NA for i in range(5))


def test_forward_return_labels_n_less_than_horizon_marks_every_row_nan():
    """``n < horizon`` — same boundary on the wrong side. Must still emit a
    correctly-sized all-NA series rather than IndexError."""
    bars = _flat_ohlc([100.0, 101.0, 102.0])
    labels = forward_return_labels(bars, LabelConfig(horizon=5))
    assert len(labels) == 3
    assert all(labels.iloc[i] is pd.NA for i in range(3))


def test_forward_return_labels_n_equals_horizon_plus_one_labels_first_row():
    """``n == horizon + 1`` — exactly one row has forward data; every other is NA."""
    closes = [100.0] * 5 + [101.0]  # h=5 → only row 0 has a forward bar
    bars = _flat_ohlc(closes)
    labels = forward_return_labels(bars, LabelConfig(horizon=5))
    assert labels.iloc[0] == 1
    for i in range(1, 6):
        assert labels.iloc[i] is pd.NA


def test_forward_return_labels_neutral_band_strict_inequality_at_boundary():
    """Forward returns *exactly* equal to ``neutral_band`` must NOT flip the
    label off zero (the band is a strict inequality threshold)."""
    # Forward return = 0.001 exactly == band → label 0.
    closes = [100.0, 100.1]
    bars = _flat_ohlc(closes)
    labels = forward_return_labels(bars, LabelConfig(horizon=1, neutral_band=0.001))
    assert labels.iloc[0] == 0


def test_forward_return_labels_validates_index_and_columns():
    # Missing close column.
    df = pd.DataFrame({"open": [1.0]}, index=pd.date_range("2026-01-01", periods=1, tz="UTC"))
    with pytest.raises(ValueError, match="forward_return_labels requires"):
        forward_return_labels(df)

    # Non-DatetimeIndex.
    df2 = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},
        index=pd.RangeIndex(1),
    )
    with pytest.raises(ValueError, match="DatetimeIndex"):
        forward_return_labels(df2)


# ----------------------------------------------------------------- triple-barrier


def _build_atr_warmed_bars(
    closes: list[float],
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> pd.DataFrame:
    n = len(closes)
    if highs is None:
        highs = closes
    if lows is None:
        lows = closes
    idx = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes},
        index=idx,
    )


def test_triple_barrier_first_touch_upper_wins_when_hit_before_lower():
    # 5-bar warmup at flat 100 to seed ATR=1 cleanly (TRs after the +/-1
    # primer are 1.0 each). Entry at t=5 (close=100).
    # ATR_5 ≈ 1.0; barrier_mult=2 → upper=102, lower=98.
    # Forward bars: t=6 high=101 (no touch); t=7 high=103 (UPPER hit, j=2);
    # t=8 low=95 would hit lower but upper was first → +1 with offset=2.
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 103.0, 95.0]
    highs = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 103.0, 95.0]
    lows = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 101.0, 103.0, 95.0]
    bars = _build_atr_warmed_bars(closes, highs, lows)

    config = TripleBarrierConfig(horizon=3, atr_period=5, barrier_mult=2.0)
    out = triple_barrier_labels(bars, config)

    # Entry row at t=5: upper hit at j=2.
    assert out["label"].iloc[5] == 1
    assert out["barrier"].iloc[5] == "upper"
    assert int(out["exit_offset"].iloc[5]) == 2

    # Pre-ATR-ready rows (0..3) are warmup; row 4 is the first ready
    # bar. We don't assert on row 4 because the ATR seed value depends
    # on the exact TR series; the production guarantee is "rows with
    # NaN ATR have label NaN", which is exercised in the warmup test.


def test_triple_barrier_first_touch_lower_wins_when_hit_before_upper():
    # Same primer; entry at t=5 (close=100). Bar 6 dips to low=97
    # (touches lower=98), bar 7 spikes to 103 (would touch upper)
    # → lower hit FIRST at j=1.
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 99.0, 103.0, 100.0]
    highs = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 99.0, 103.0, 100.0]
    lows = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 97.0, 103.0, 100.0]
    bars = _build_atr_warmed_bars(closes, highs, lows)

    config = TripleBarrierConfig(horizon=3, atr_period=5, barrier_mult=2.0)
    out = triple_barrier_labels(bars, config)

    assert out["label"].iloc[5] == -1
    assert out["barrier"].iloc[5] == "lower"
    assert int(out["exit_offset"].iloc[5]) == 1


def test_triple_barrier_vertical_barrier_when_no_touch():
    # Entry at t=5; bars 6,7,8 stay near 100 (no touch) and end at 100.5
    # → net +0.5% > 0 with vertical_band=0 → label +1, barrier=vertical.
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 100.2, 100.3, 100.5]
    highs = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 100.4, 100.5, 100.7]
    lows = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 100.0, 100.1, 100.3]
    bars = _build_atr_warmed_bars(closes, highs, lows)

    config = TripleBarrierConfig(horizon=3, atr_period=5, barrier_mult=2.0)
    out = triple_barrier_labels(bars, config)

    assert out["label"].iloc[5] == 1  # net positive at vertical barrier
    assert out["barrier"].iloc[5] == "vertical"
    assert int(out["exit_offset"].iloc[5]) == 3


def test_triple_barrier_vertical_band_zeroes_small_moves():
    # Vertical-barrier scenario but band > net move → label 0.
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 100.05, 100.1, 100.05]
    highs = closes[:]
    lows = closes[:]
    bars = _build_atr_warmed_bars(closes, highs, lows)

    config = TripleBarrierConfig(horizon=3, atr_period=5, barrier_mult=5.0, vertical_band=0.001)
    out = triple_barrier_labels(bars, config)

    assert out["label"].iloc[5] == 0
    assert out["barrier"].iloc[5] == "vertical"


def test_triple_barrier_ambiguous_when_both_in_same_bar():
    # Single forward bar covers entire ATR-scaled range → both barriers
    # touched in bar j=1 → ambiguous, label=0.
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 100.0]
    highs = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 103.0]
    lows = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 97.0]
    bars = _build_atr_warmed_bars(closes, highs, lows)

    config = TripleBarrierConfig(horizon=1, atr_period=5, barrier_mult=2.0)
    out = triple_barrier_labels(bars, config)

    assert out["label"].iloc[5] == 0
    assert out["barrier"].iloc[5] == "ambiguous"
    assert int(out["exit_offset"].iloc[5]) == 1


def test_triple_barrier_warmup_and_tail_marked_nan():
    # Strictly-monotone closes mean each TR after the first equals 1.0,
    # so ATR with period=3 is ready from t=2 onwards.
    closes = list(range(10))
    bars = _build_atr_warmed_bars([float(c) for c in closes])

    config = TripleBarrierConfig(horizon=2, atr_period=3, barrier_mult=2.0)
    out = triple_barrier_labels(bars, config)

    # ATR is NaN at t=0 and t=1 (need 3 TRs) → warmup
    for i in (0, 1):
        assert out["label"].iloc[i] is pd.NA
        assert out["barrier"].iloc[i] == "warmup"

    # Trailing horizon=2 rows (8, 9) — insufficient forward data
    for i in (8, 9):
        assert out["label"].iloc[i] is pd.NA
        assert out["barrier"].iloc[i] == "warmup"

    # Middle rows have valid labels (ATR ready and forward data present)
    for i in range(2, 8):
        assert out["label"].iloc[i] is not pd.NA, f"row {i} should be labelled"


def test_triple_barrier_config_validation():
    with pytest.raises(ValueError, match="horizon"):
        TripleBarrierConfig(horizon=0)
    with pytest.raises(ValueError, match="atr_period"):
        TripleBarrierConfig(atr_period=0)
    with pytest.raises(ValueError, match="barrier_mult"):
        TripleBarrierConfig(barrier_mult=0.0)
    with pytest.raises(ValueError, match="upper_mult"):
        TripleBarrierConfig(upper_mult=-1.0)
    with pytest.raises(ValueError, match="lower_mult"):
        TripleBarrierConfig(lower_mult=-1.0)
    with pytest.raises(ValueError, match="vertical_band"):
        TripleBarrierConfig(vertical_band=-0.001)


def test_triple_barrier_n_too_small_emits_all_warmup_rows():
    """Dataset shorter than ``atr_period + horizon`` cannot label anything."""
    closes = [100.0, 100.5, 99.5, 100.0]  # n=4, atr=5 → no row ever ATR-ready
    bars = _build_atr_warmed_bars(closes)
    out = triple_barrier_labels(bars, TripleBarrierConfig(horizon=1, atr_period=5))
    assert len(out) == 4
    for i in range(4):
        assert out["label"].iloc[i] is pd.NA
        assert out["barrier"].iloc[i] == "warmup"


def test_triple_barrier_vertical_band_strict_inequality_at_boundary():
    """Vertical-barrier labels: net move equal to ``vertical_band`` is treated
    as flat (band uses strict inequalities), not as a directional label."""
    # Wide barrier_mult so neither upper nor lower is touched; pick closes so
    # the net forward return at horizon equals vertical_band exactly.
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 100.05, 100.05, 100.10]
    highs = closes[:]
    lows = closes[:]
    bars = _build_atr_warmed_bars(closes, highs, lows)
    # Net forward return at t=5 over horizon=3 = 100.10/100.0 - 1 = 0.001.
    config = TripleBarrierConfig(
        horizon=3, atr_period=5, barrier_mult=20.0, vertical_band=0.001
    )
    out = triple_barrier_labels(bars, config)
    assert out["label"].iloc[5] == 0
    assert out["barrier"].iloc[5] == "vertical"


def test_triple_barrier_asymmetric_overrides():
    # Lower mult tiny → lower barrier touched easily; upper mult huge → never
    closes = [99.0, 100.0, 101.0, 100.0, 101.0, 100.0, 99.99, 99.99, 99.99]
    highs = closes[:]
    lows = closes[:]
    bars = _build_atr_warmed_bars(closes, highs, lows)

    config = TripleBarrierConfig(
        horizon=3,
        atr_period=5,
        barrier_mult=2.0,
        upper_mult=100.0,
        lower_mult=0.001,
    )
    out = triple_barrier_labels(bars, config)

    assert out["label"].iloc[5] == -1
    assert out["barrier"].iloc[5] == "lower"


# --------------------------------------------------------------- class balance


def test_class_balance_report_counts_and_proportions():
    fold_a = pd.Series([1, 1, -1, 0, 0, 0, pd.NA, pd.NA], dtype="Int8")
    fold_b = pd.Series([1, 1, 1, 1, -1, 0], dtype="Int8")

    report = class_balance_report({"fold_a": fold_a, "fold_b": fold_b})

    # Index and columns
    assert list(report.index) == ["fold_a", "fold_b"]
    assert list(report.columns) == [
        "n",
        "count_-1",
        "count_0",
        "count_1",
        "prop_-1",
        "prop_0",
        "prop_1",
    ]

    # fold_a (NaNs dropped → n=6): 1 down, 3 zero, 2 up
    assert report.loc["fold_a", "n"] == 6
    assert report.loc["fold_a", "count_-1"] == 1
    assert report.loc["fold_a", "count_0"] == 3
    assert report.loc["fold_a", "count_1"] == 2
    assert report.loc["fold_a", "prop_-1"] == pytest.approx(1 / 6)
    assert report.loc["fold_a", "prop_0"] == pytest.approx(3 / 6)
    assert report.loc["fold_a", "prop_1"] == pytest.approx(2 / 6)

    # fold_b: 1 down, 1 zero, 4 up; n=6
    assert report.loc["fold_b", "n"] == 6
    assert report.loc["fold_b", "count_1"] == 4
    assert report.loc["fold_b", "prop_1"] == pytest.approx(4 / 6)


def test_class_balance_report_empty_fold():
    fold = pd.Series([pd.NA, pd.NA], dtype="Int8")
    report = class_balance_report({"empty": fold})
    assert report.loc["empty", "n"] == 0
    assert report.loc["empty", "count_-1"] == 0
    assert report.loc["empty", "prop_-1"] == 0.0


def test_print_class_balance_returns_same_dataframe(capsys):
    fold_a = pd.Series([1, 0, -1], dtype="Int8")
    out = print_class_balance({"fold_a": fold_a})
    captured = capsys.readouterr()
    assert "fold_a" in captured.out
    assert "count_1" in captured.out
    assert out.loc["fold_a", "n"] == 3
