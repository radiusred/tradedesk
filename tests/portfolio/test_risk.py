"""Tests for risk management utilities."""

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
