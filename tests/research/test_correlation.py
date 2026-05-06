"""Tests for the correlation gate calculator."""

from __future__ import annotations

from datetime import date

import pytest

from tradedesk.recording import RoundTrip
from tradedesk.research import (
    CorrelationResult,
    correlation_gate,
    daily_pnl_from_round_trips,
    daily_pnl_from_trade_rows,
    pearson,
)
from tradedesk.research.correlation import _aligned, _parse_date


def test_pearson_perfect_positive():
    assert pearson([1.0, 2.0, 3.0], [2.0, 4.0, 6.0]) == pytest.approx(1.0)


def test_pearson_perfect_negative():
    assert pearson([1.0, 2.0, 3.0], [-1.0, -2.0, -3.0]) == pytest.approx(-1.0)


def test_pearson_zero_variance_returns_zero():
    # Constant series has no variance — gate interprets as not-correlated.
    assert pearson([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) == 0.0
    assert pearson([], []) == 0.0
    assert pearson([1.0], [2.0]) == 0.0


def test_pearson_uncorrelated_noise():
    xs = [1.0, -1.0, 1.0, -1.0]
    ys = [1.0, 1.0, -1.0, -1.0]
    assert pearson(xs, ys) == pytest.approx(0.0)


def test_pearson_length_mismatch_raises():
    with pytest.raises(ValueError, match="equal length"):
        pearson([1.0, 2.0], [1.0])


def test_correlation_gate_flags_above_threshold():
    candidate = {date(2024, 1, d): float(d) for d in range(1, 31)}
    # Sleeve A: same direction, will be 1.0 correlated → flagged.
    sleeve_a = {date(2024, 1, d): float(d) * 2 for d in range(1, 31)}
    # Sleeve B: anti-correlated, |corr|=1 → flagged.
    sleeve_b = {date(2024, 1, d): -float(d) for d in range(1, 31)}
    # Sleeve C: orthogonal-ish noise — small magnitude correlation.
    sleeve_c = {
        date(2024, 1, d): (1.0 if d % 2 else -1.0) for d in range(1, 31)
    }

    res = correlation_gate(
        "candidate",
        candidate,
        {"A": sleeve_a, "B": sleeve_b, "C": sleeve_c},
    )

    assert isinstance(res, CorrelationResult)
    assert res.candidate == "candidate"
    assert res.candidate_vs_sleeves["A"] == pytest.approx(1.0)
    assert res.candidate_vs_sleeves["B"] == pytest.approx(-1.0)
    flagged_names = {name for name, _ in res.flagged}
    assert flagged_names == {"A", "B"}
    assert res.fails_gate is True
    # flagged is sorted by descending |corr|.
    assert abs(res.flagged[0][1]) >= abs(res.flagged[1][1])


def test_correlation_gate_passes_when_uncorrelated():
    candidate = {date(2024, 1, d): float(d) for d in range(1, 31)}
    sleeve = {
        date(2024, 1, d): (1.0 if d % 2 else -1.0) for d in range(1, 31)
    }
    res = correlation_gate("candidate", candidate, {"A": sleeve})
    assert res.fails_gate is False
    assert res.flagged == []
    assert "A" in res.candidate_vs_sleeves


def test_correlation_gate_skips_low_overlap_sleeves():
    candidate = {date(2024, 1, d): float(d) for d in range(1, 31)}
    # Only 5 overlapping days — below the default min_overlap_days (20).
    sleeve = {date(2024, 1, d): float(d) for d in range(1, 6)}
    res = correlation_gate("candidate", candidate, {"low": sleeve})
    assert "low" in res.skipped_sleeves
    assert "low" not in res.candidate_vs_sleeves
    assert res.overlap_days["low"] == 5
    assert res.fails_gate is False


def test_correlation_gate_aligns_on_intersection_only():
    # Candidate and sleeve share only 3 days; with min_overlap_days=2 the
    # silent days on either side are skipped, not zero-filled.
    candidate = {
        date(2024, 1, 1): 1.0,
        date(2024, 1, 2): 2.0,
        date(2024, 1, 3): 3.0,
        date(2024, 1, 4): 4.0,  # candidate-only
    }
    sleeve = {
        date(2024, 1, 2): 4.0,
        date(2024, 1, 3): 6.0,
        date(2024, 1, 5): 99.0,  # sleeve-only — must be ignored
    }
    res = correlation_gate(
        "c", candidate, {"S": sleeve}, min_overlap_days=2
    )
    # Aligned on 2024-01-02 and 2024-01-03 only → perfect correlation.
    assert res.candidate_vs_sleeves["S"] == pytest.approx(1.0)
    assert res.overlap_days["S"] == 2


def test_correlation_gate_threshold_validation():
    with pytest.raises(ValueError, match="threshold"):
        correlation_gate("c", {}, {}, threshold=0.0)
    with pytest.raises(ValueError, match="threshold"):
        correlation_gate("c", {}, {}, threshold=1.5)
    with pytest.raises(ValueError, match="min_overlap_days"):
        correlation_gate("c", {}, {}, min_overlap_days=1)


def test_correlation_gate_threshold_changes_verdict():
    candidate = {date(2024, 1, d): float(d) for d in range(1, 31)}
    sleeve = {date(2024, 1, d): float(d) * 0.5 for d in range(1, 31)}
    # Perfect correlation: any threshold <= 1.0 fires.
    assert correlation_gate("c", candidate, {"S": sleeve}, threshold=0.6).fails_gate
    assert correlation_gate("c", candidate, {"S": sleeve}, threshold=0.99).fails_gate
    # Threshold of 1.0 still fires (>= comparison).
    assert correlation_gate("c", candidate, {"S": sleeve}, threshold=1.0).fails_gate


def test_correlation_gate_sleeve_matrix_diagonal_is_one():
    candidate = {date(2024, 1, d): float(d) for d in range(1, 31)}
    sleeve_a = {date(2024, 1, d): float(d) for d in range(1, 31)}
    sleeve_b = {date(2024, 1, d): -float(d) for d in range(1, 31)}
    res = correlation_gate(
        "c", candidate, {"A": sleeve_a, "B": sleeve_b}
    )
    assert res.sleeve_vs_sleeve["A"]["A"] == 1.0
    assert res.sleeve_vs_sleeve["B"]["B"] == 1.0
    # Symmetric.
    assert res.sleeve_vs_sleeve["A"]["B"] == pytest.approx(
        res.sleeve_vs_sleeve["B"]["A"]
    )
    assert res.sleeve_vs_sleeve["A"]["B"] == pytest.approx(-1.0)


def test_daily_pnl_from_round_trips_buckets_by_exit_date():
    trips = [
        RoundTrip(
            instrument="EURUSD",
            direction="BUY",
            entry_ts="2024-01-01T09:00:00Z",
            exit_ts="2024-01-02T15:00:00Z",
            entry_price=1.10,
            exit_price=1.11,
            size=1.0,
            pnl=10.0,
        ),
        RoundTrip(
            instrument="EURUSD",
            direction="SELL",
            entry_ts="2024-01-02T09:00:00Z",
            exit_ts="2024-01-02T16:30:00Z",
            entry_price=1.12,
            exit_price=1.11,
            size=1.0,
            pnl=5.0,
        ),
        RoundTrip(
            instrument="USDJPY",
            direction="BUY",
            entry_ts="2024-01-03T09:00:00Z",
            exit_ts="2024-01-04T09:00:00Z",
            entry_price=150.0,
            exit_price=151.0,
            size=1.0,
            pnl=-3.0,
        ),
    ]
    daily = daily_pnl_from_round_trips(trips)
    assert daily[date(2024, 1, 2)] == pytest.approx(15.0)
    assert daily[date(2024, 1, 4)] == pytest.approx(-3.0)
    assert date(2024, 1, 1) not in daily
    assert date(2024, 1, 3) not in daily


def test_daily_pnl_from_trade_rows_pairs_buy_sell_fills():
    rows = [
        {
            "instrument": "EURUSD",
            "direction": "BUY",
            "timestamp": "2024-01-01T10:00:00Z",
            "price": "1.10",
            "size": "1.0",
        },
        {
            "instrument": "EURUSD",
            "direction": "SELL",
            "timestamp": "2024-01-02T15:00:00Z",
            "price": "1.12",
            "size": "1.0",
        },
    ]
    daily = daily_pnl_from_trade_rows(rows)
    # Long EURUSD +0.02 × size 1.0 → 0.02 pnl bucket on exit date.
    assert pytest.approx(daily[date(2024, 1, 2)], rel=1e-6) == 0.02


def test_aligned_helper_returns_intersection_in_date_order():
    a = {date(2024, 1, 1): 1.0, date(2024, 1, 3): 3.0, date(2024, 1, 2): 2.0}
    b = {date(2024, 1, 2): 20.0, date(2024, 1, 3): 30.0}
    xs, ys = _aligned(a, b)
    assert xs == [2.0, 3.0]
    assert ys == [20.0, 30.0]


def test_parse_date_accepts_date_datetime_and_strings():
    from datetime import datetime

    assert _parse_date(date(2024, 5, 1)) == date(2024, 5, 1)
    assert _parse_date(datetime(2024, 5, 1, 12, 0)) == date(2024, 5, 1)
    assert _parse_date("2024-05-01") == date(2024, 5, 1)
    assert _parse_date("2024-05-01T09:30:00Z") == date(2024, 5, 1)
    assert _parse_date("2024/05/01") == date(2024, 5, 1)


def test_correlation_gate_no_sleeves():
    candidate = {date(2024, 1, d): float(d) for d in range(1, 30)}
    res = correlation_gate("solo", candidate, {})
    assert res.candidate_vs_sleeves == {}
    assert res.sleeve_vs_sleeve == {}
    assert res.flagged == []
    assert res.fails_gate is False
