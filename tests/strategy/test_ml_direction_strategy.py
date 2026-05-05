"""Tests for :mod:`tradedesk.strategy.ml_direction_strategy` (Phase 6)."""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

from tradedesk.events import get_dispatcher, reset_dispatcher
from tradedesk.marketdata import CandleClosedEvent
from tradedesk.ml import FeatureBuilder, FeatureConfig
from tradedesk.strategy import (
    MLDirectionConfig,
    MLDirectionStrategy,
    Signal,
    SignalGeneratedEvent,
    probability_to_signal,
)
from tradedesk.types import Candle

# ----------------------------------------------------------- threshold mapping


class TestProbabilityToSignal:
    """probability_to_signal must respect threshold + neutral band."""

    def test_long_at_threshold_boundary(self) -> None:
        assert probability_to_signal(0.6, threshold=0.6) is Signal.ENTRY_LONG

    def test_long_above_threshold(self) -> None:
        assert probability_to_signal(0.95, threshold=0.55) is Signal.ENTRY_LONG

    def test_short_at_inverse_boundary(self) -> None:
        assert probability_to_signal(0.4, threshold=0.6) is Signal.ENTRY_SHORT

    def test_short_below_inverse(self) -> None:
        assert probability_to_signal(0.05, threshold=0.55) is Signal.ENTRY_SHORT

    @pytest.mark.parametrize("p", [0.41, 0.5, 0.55, 0.59])
    def test_neutral_band(self, p: float) -> None:
        assert probability_to_signal(p, threshold=0.6) is Signal.NEUTRAL

    def test_threshold_half_collapses_neutral_band(self) -> None:
        # At threshold == 0.5 the only neutral point is exactly 0.5; everything
        # above goes long, everything below goes short.
        assert probability_to_signal(0.5, threshold=0.5) is Signal.ENTRY_LONG
        assert probability_to_signal(0.50001, threshold=0.5) is Signal.ENTRY_LONG
        assert probability_to_signal(0.49999, threshold=0.5) is Signal.ENTRY_SHORT

    def test_invalid_threshold(self) -> None:
        with pytest.raises(ValueError):
            probability_to_signal(0.7, threshold=0.49)
        with pytest.raises(ValueError):
            probability_to_signal(0.7, threshold=1.01)

    def test_invalid_probability(self) -> None:
        with pytest.raises(ValueError):
            probability_to_signal(-0.01)
        with pytest.raises(ValueError):
            probability_to_signal(1.01)


# ------------------------------------------------------------------ config


class TestMLDirectionConfig:
    def test_invalid_threshold_rejected(self) -> None:
        with pytest.raises(ValueError):
            MLDirectionConfig(threshold=0.49)
        with pytest.raises(ValueError):
            MLDirectionConfig(threshold=1.01)

    def test_invalid_history_capacity_rejected(self) -> None:
        with pytest.raises(ValueError):
            MLDirectionConfig(history_capacity=0)


# ----------------------------------------------------------- end-to-end stub


class _StubModel:
    """Always returns the configured (down, up) probability tuple."""

    def __init__(self, p_up: float) -> None:
        self.p_up = p_up
        self.calls: list[pd.DataFrame] = []

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        self.calls.append(X)
        n = len(X)
        return np.tile([1.0 - self.p_up, self.p_up], (n, 1))


def _synthetic_bars(
    n: int,
    seed: int = 0,
    start: str = "2024-01-01T00:00:00",
) -> list[Candle]:
    rng = np.random.default_rng(seed)
    base = 1.10
    candles: list[Candle] = []
    ts = pd.Timestamp(start, tz="UTC")
    for i in range(n):
        step = float(rng.normal(0.0, 1e-4))
        base = base * (1.0 + step)
        high = base * (1.0 + abs(rng.normal(0.0, 5e-5)))
        low = base * (1.0 - abs(rng.normal(0.0, 5e-5)))
        candles.append(
            Candle(
                timestamp=(ts + pd.Timedelta(minutes=i)).isoformat(),
                open=base,
                high=max(base, high),
                low=min(base, low),
                close=base,
                volume=100.0,
            )
        )
    return candles


def _candle_event(c: Candle, instrument: str, period: str) -> CandleClosedEvent:
    return CandleClosedEvent(
        instrument=instrument,
        timeframe=period,
        candle=c,
    )


class TestMLDirectionStrategy:
    """Smoke-tests the streaming loop end-to-end with a stub model."""

    def setup_method(self) -> None:
        reset_dispatcher()

    def teardown_method(self) -> None:
        reset_dispatcher()

    def test_emits_long_signal_when_model_is_bullish(self) -> None:
        instrument = "CS.D.EURUSD.CFD.IP"
        period = "1MIN"
        # Tight window so warmup is small in the unit test (~30 bars).
        feat_cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        builder = FeatureBuilder(config=feat_cfg, indicators={})
        model = _StubModel(p_up=0.95)
        strategy = MLDirectionStrategy(
            instrument=instrument,
            period=period,
            feature_builder=builder,
            model=model,
            config=MLDirectionConfig(threshold=0.6, history_capacity=512),
        )

        seen: list[SignalGeneratedEvent] = []
        get_dispatcher().subscribe(SignalGeneratedEvent, lambda e: seen.append(e))

        for c in _synthetic_bars(60):
            asyncio.run(
                strategy.on_candle_close(_candle_event(c, instrument, period))
            )

        assert seen, "expected at least one signal after warmup"
        assert seen[-1].signal is Signal.ENTRY_LONG
        assert strategy.last_p_up == pytest.approx(0.95)
        assert strategy.last_signal is Signal.ENTRY_LONG

    def test_emits_short_signal_when_model_is_bearish(self) -> None:
        instrument = "CS.D.EURUSD.CFD.IP"
        period = "1MIN"
        feat_cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        builder = FeatureBuilder(config=feat_cfg, indicators={})
        model = _StubModel(p_up=0.05)
        strategy = MLDirectionStrategy(
            instrument=instrument,
            period=period,
            feature_builder=builder,
            model=model,
            config=MLDirectionConfig(threshold=0.55, history_capacity=512),
        )

        seen: list[SignalGeneratedEvent] = []
        get_dispatcher().subscribe(SignalGeneratedEvent, lambda e: seen.append(e))

        for c in _synthetic_bars(40):
            asyncio.run(
                strategy.on_candle_close(_candle_event(c, instrument, period))
            )

        assert seen[-1].signal is Signal.ENTRY_SHORT
        assert strategy.last_signal is Signal.ENTRY_SHORT

    def test_no_signal_before_warmup(self) -> None:
        instrument = "CS.D.EURUSD.CFD.IP"
        period = "1MIN"
        feat_cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        builder = FeatureBuilder(config=feat_cfg, indicators={})
        model = _StubModel(p_up=0.95)
        strategy = MLDirectionStrategy(
            instrument=instrument,
            period=period,
            feature_builder=builder,
            model=model,
            config=MLDirectionConfig(threshold=0.55),
        )
        seen: list[SignalGeneratedEvent] = []
        get_dispatcher().subscribe(SignalGeneratedEvent, lambda e: seen.append(e))

        # Feed exactly warmup bars — strategy should not emit yet.
        warmup = builder.warmup()
        for c in _synthetic_bars(warmup):
            asyncio.run(
                strategy.on_candle_close(_candle_event(c, instrument, period))
            )
        assert seen == []
        assert model.calls == []

    def test_warmup_from_history_primes_buffer(self) -> None:
        instrument = "CS.D.EURUSD.CFD.IP"
        period = "1MIN"
        feat_cfg = FeatureConfig(
            return_windows=(1, 5),
            moment_windows=(15,),
            include_microstructure=False,
            include_time_features=False,
        )
        builder = FeatureBuilder(config=feat_cfg, indicators={})
        model = _StubModel(p_up=0.99)
        strategy = MLDirectionStrategy(
            instrument=instrument,
            period=period,
            feature_builder=builder,
            model=model,
            config=MLDirectionConfig(threshold=0.6, history_capacity=512),
        )
        seen: list[SignalGeneratedEvent] = []
        get_dispatcher().subscribe(SignalGeneratedEvent, lambda e: seen.append(e))

        n_history = builder.warmup() + 5
        history_bars = _synthetic_bars(n_history)
        strategy.warmup_from_history({(instrument, period): history_bars})

        # First post-warmup live candle should now be enough to score; offset
        # the timestamp past the warmup tail so the index stays monotonic.
        live = _synthetic_bars(
            1,
            seed=1,
            start=(pd.Timestamp("2024-01-01", tz="UTC")
                   + pd.Timedelta(minutes=n_history)).isoformat(),
        )[0]
        asyncio.run(strategy.on_candle_close(_candle_event(live, instrument, period)))

        assert len(seen) == 1
        assert seen[0].signal is Signal.ENTRY_LONG
