"""Tests for risk management utilities."""

import pytest

from tradedesk.portfolio.risk import atr_normalised_size


def test_atr_normalised_size_decreases_as_atr_increases():
    """Larger ATR should result in smaller position size, all else equal."""
    s1 = atr_normalised_size(
        risk_per_trade=100.0, atr=1.0, atr_risk_mult=1.0, min_size=0.1, max_size=100.0
    )
    s2 = atr_normalised_size(
        risk_per_trade=100.0, atr=2.0, atr_risk_mult=1.0, min_size=0.1, max_size=100.0
    )
    assert s2 < s1


def test_atr_normalised_size_is_clamped():
    """Position size should be clamped to min/max bounds."""
    # Very small ATR would imply huge size -> clamp to max_size
    s = atr_normalised_size(
        risk_per_trade=100.0, atr=0.0001, atr_risk_mult=1.0, min_size=0.1, max_size=10.0
    )
    assert s == 10.0

    # Very large ATR would imply tiny size -> clamp to min_size
    s = atr_normalised_size(
        risk_per_trade=100.0, atr=1e9, atr_risk_mult=1.0, min_size=0.1, max_size=10.0
    )
    assert s == 0.1


def test_atr_normalised_size_default_point_value_unchanged():
    """Default point_value=1.0 must reproduce the pre-RAD-688 formula exactly."""
    legacy = atr_normalised_size(
        risk_per_trade=100.0,
        atr=2.5,
        atr_risk_mult=1.5,
        min_size=0.1,
        max_size=100.0,
    )
    explicit = atr_normalised_size(
        risk_per_trade=100.0,
        atr=2.5,
        atr_risk_mult=1.5,
        min_size=0.1,
        max_size=100.0,
        point_value=1.0,
    )
    assert legacy == pytest.approx(explicit)
    # Hand-computed: 100 / (2.5 * 1.5) ≈ 26.667
    assert legacy == pytest.approx(100.0 / (2.5 * 1.5))


def test_atr_normalised_size_unit_independent_for_xauusd():
    """RAD-688: same contract count for LIVE-units and Dukascopy-cents.

    LIVE IG XAUUSD feed: gold ≈ $4,580, ATR ≈ 8.9, point_value = 1.0.
    Dukascopy cache:    gold ≈ 458,000 cents, ATR ≈ 890, point_value = 0.01.

    Both pipes must produce the same size for the same risk_per_trade.
    """
    risk_per_trade = 1000.0
    atr_risk_mult = 1.0

    live_size = atr_normalised_size(
        risk_per_trade=risk_per_trade,
        atr=8.9,
        atr_risk_mult=atr_risk_mult,
        min_size=1.0,
        max_size=1000.0,
        point_value=1.0,
    )
    backtest_size = atr_normalised_size(
        risk_per_trade=risk_per_trade,
        atr=890.0,
        atr_risk_mult=atr_risk_mult,
        min_size=1.0,
        max_size=1000.0,
        point_value=0.01,
    )

    assert live_size == pytest.approx(backtest_size)
    # Hand-computed: 1000 / (8.9 * 1.0 * 1.0) ≈ 112.36
    assert live_size == pytest.approx(1000.0 / 8.9)


def test_atr_normalised_size_point_value_zero_falls_back_to_min():
    """Guard: a misconfigured point_value=0 must not crash (denom <= 0 path)."""
    s = atr_normalised_size(
        risk_per_trade=100.0,
        atr=2.0,
        atr_risk_mult=1.0,
        min_size=0.5,
        max_size=10.0,
        point_value=0.0,
    )
    assert s == 0.5
