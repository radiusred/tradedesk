"""ML-driven direction strategy (Phase 6 / RAD-896 / RAD-903).

Streams 1-minute candles through a trained probability model and emits a
long/flat/short :class:`Signal` on each candle close. The strategy is the
runtime glue between the offline Phase 6 ML stack
(:class:`tradedesk.ml.FeatureBuilder` plus any model exposing
``predict_proba``) and the existing portfolio risk-controls.

Position sizing is **not** the strategy's concern: signals are dispatched
via :class:`SignalGeneratedEvent` for the portfolio orchestrator (typically
the ATR-aware sleeve in ``ig_trader``) to size and execute.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from tradedesk.events import get_dispatcher
from tradedesk.marketdata import CandleClosedEvent, ChartSubscription
from tradedesk.ml.features import FeatureBuilder
from tradedesk.types import Candle, DataProvider

from .base import BaseStrategy, Signal
from .events import SignalGeneratedEvent

log = logging.getLogger(__name__)


__all__ = [
    "MLDirectionConfig",
    "MLDirectionStrategy",
    "ProbabilityModel",
    "probability_to_signal",
]


class ProbabilityModel(Protocol):
    """Minimal model interface required by :class:`MLDirectionStrategy`.

    Any object exposing ``predict_proba(X)`` returning an ``(n, 2)`` array
    where column ``1`` is the up-probability satisfies the protocol —
    :class:`tradedesk.ml.model.DirectionClassifier`, ``sklearn`` binary
    classifiers, or a hand-rolled stub all work.
    """

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray: ...


@dataclass(frozen=True)
class MLDirectionConfig:
    """Configuration for :class:`MLDirectionStrategy`.

    Attributes:
        threshold: Probability threshold for an actionable signal. Must
            satisfy ``0.5 <= threshold <= 1.0``. Probabilities ``>=
            threshold`` emit ``ENTRY_LONG``, probabilities ``<= 1 -
            threshold`` emit ``ENTRY_SHORT``, anything else stays
            ``NEUTRAL``. ``threshold == 0.5`` collapses the neutral band
            and always emits a directional signal.
        history_capacity: Rolling window size kept for feature
            computation. Must exceed
            :meth:`tradedesk.ml.FeatureBuilder.warmup` or the strategy
            will never emit a signal.
    """

    threshold: float = 0.55
    history_capacity: int = 1024

    def __post_init__(self) -> None:
        if not 0.5 <= self.threshold <= 1.0:
            raise ValueError("threshold must satisfy 0.5 <= threshold <= 1.0")
        if self.history_capacity < 1:
            raise ValueError("history_capacity must be >= 1")


def probability_to_signal(p_up: float, threshold: float = 0.55) -> Signal:
    """Map an up-probability to a long / flat / short :class:`Signal`.

    Args:
        p_up: Model-estimated probability of an up move. Must lie in
            ``[0.0, 1.0]``.
        threshold: Probability threshold for an actionable signal. Must
            satisfy ``0.5 <= threshold <= 1.0``.

    Returns:
        * :data:`Signal.ENTRY_LONG` when ``p_up >= threshold``
        * :data:`Signal.ENTRY_SHORT` when ``p_up <= 1 - threshold``
        * :data:`Signal.NEUTRAL` otherwise

    Raises:
        ValueError: ``p_up`` outside ``[0, 1]`` or ``threshold`` outside
            ``[0.5, 1.0]``.
    """
    if not 0.5 <= threshold <= 1.0:
        raise ValueError("threshold must satisfy 0.5 <= threshold <= 1.0")
    if not 0.0 <= p_up <= 1.0:
        raise ValueError("p_up must satisfy 0.0 <= p_up <= 1.0")
    if p_up >= threshold:
        return Signal.ENTRY_LONG
    if p_up <= 1.0 - threshold:
        return Signal.ENTRY_SHORT
    return Signal.NEUTRAL


class MLDirectionStrategy(BaseStrategy):
    """Streaming ML-driven direction strategy.

    On each :class:`CandleClosedEvent` the strategy:

    1. Appends the candle to a rolling history buffer (also primed via
       :meth:`warmup_from_history`).
    2. Calls :meth:`FeatureBuilder.transform` on the buffer and takes the
       last (current) feature row. Signals are suppressed until the
       buffer exceeds :meth:`FeatureBuilder.warmup`.
    3. Calls ``model.predict_proba`` on the latest row and stores
       ``last_p_up`` for downstream introspection.
    4. Maps the probability to a :class:`Signal` via
       :func:`probability_to_signal` and dispatches a
       :class:`SignalGeneratedEvent` so the portfolio orchestrator can
       size and execute (ATR-based sleeve sizing is reused unchanged).
    """

    def __init__(
        self,
        *,
        instrument: str,
        period: str,
        feature_builder: FeatureBuilder,
        model: ProbabilityModel,
        config: MLDirectionConfig | None = None,
        strategy_id: str | None = None,
        data_provider: DataProvider | None = None,
    ) -> None:
        super().__init__(
            data_provider=data_provider,
            subscriptions=[ChartSubscription(instrument, period)],
        )
        self.instrument = instrument
        self.period = period
        self.feature_builder = feature_builder
        self.model = model
        self.config = config or MLDirectionConfig()
        self.strategy_id = strategy_id or self.__class__.__name__
        self._history: deque[Candle] = deque(maxlen=self.config.history_capacity)
        self.last_signal: Signal | None = None
        self.last_p_up: float | None = None

    def warmup_from_history(
        self, history: dict[tuple[str, str], list[Candle]]
    ) -> None:
        """Prime chart history *and* the feature buffer without firing signals."""
        super().warmup_from_history(history)
        candles = history.get((self.instrument, self.period), [])
        for c in candles[-self.config.history_capacity :]:
            self._history.append(c)

    async def on_candle_close(self, candle_close: CandleClosedEvent) -> None:
        """Update history, score the model, and dispatch a signal."""
        await super().on_candle_close(candle_close)
        self._history.append(candle_close.candle)

        if len(self._history) <= self.feature_builder.warmup():
            return

        bars_df = self._history_to_frame()
        try:
            features = self.feature_builder.transform(bars_df)
        except Exception:
            log.exception("FeatureBuilder.transform failed; skipping signal")
            return
        if features.empty:
            return

        proba = np.asarray(self.model.predict_proba(features.iloc[[-1]]))
        if proba.ndim != 2 or proba.shape[1] != 2:
            log.warning(
                "model.predict_proba returned shape %s; expected (n, 2)",
                proba.shape,
            )
            return
        p_up = float(proba[0, 1])
        signal = probability_to_signal(p_up, threshold=self.config.threshold)
        self.last_p_up = p_up
        self.last_signal = signal
        await get_dispatcher().publish(
            SignalGeneratedEvent(
                strategy_id=self.strategy_id,
                instrument=self.instrument,
                signal=signal,
            )
        )

    def _history_to_frame(self) -> pd.DataFrame:
        candles = list(self._history)
        timestamps = pd.to_datetime([c.timestamp for c in candles], utc=True)
        return pd.DataFrame(
            {
                "open": [c.open for c in candles],
                "high": [c.high for c in candles],
                "low": [c.low for c in candles],
                "close": [c.close for c in candles],
                "volume": [c.volume for c in candles],
            },
            index=pd.DatetimeIndex(timestamps, name="timestamp"),
        )
