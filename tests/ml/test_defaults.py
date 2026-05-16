"""Tests for :mod:`tradedesk.ml.defaults`."""

from __future__ import annotations

from tradedesk.ml.defaults import (
    LEAKAGE_SANITY_LEAK_NOISE,
    LEAKAGE_SANITY_THRESHOLD_ACCURACY,
    PORTFOLIO_WATCHDOG_THRESHOLD_S,
)


def test_leakage_sanity_leak_noise_is_small_positive() -> None:
    assert 0.0 < LEAKAGE_SANITY_LEAK_NOISE < 1.0


def test_leakage_sanity_threshold_accuracy_is_valid_ratio() -> None:
    assert 0.0 < LEAKAGE_SANITY_THRESHOLD_ACCURACY <= 1.0


def test_portfolio_watchdog_threshold_is_positive() -> None:
    assert PORTFOLIO_WATCHDOG_THRESHOLD_S > 0.0


def test_constants_match_original_hardcoded_values() -> None:
    assert LEAKAGE_SANITY_LEAK_NOISE == 0.05
    assert LEAKAGE_SANITY_THRESHOLD_ACCURACY == 0.95
    assert PORTFOLIO_WATCHDOG_THRESHOLD_S == 60.0
