"""Base class for technical indicators."""

import abc

from tradedesk.marketdata import Candle


class Indicator(abc.ABC):
    """Abstract base class for all technical indicators."""

    @abc.abstractmethod
    def update(self, candle: Candle) -> float | dict[str, float | None] | None:
        """Update indicator state with a new candle and return the latest value(s)."""
        raise NotImplementedError

    @abc.abstractmethod
    def ready(self) -> bool:
        """Return True when the indicator has enough data to produce valid outputs."""
        raise NotImplementedError

    @abc.abstractmethod
    def reset(self) -> None:
        """Reset indicator internal state to its initial (empty) condition."""
        raise NotImplementedError

    def warmup_periods(self) -> int:
        """
        Number of *completed candles* required before ready() can become True.
        Default 0 for backwards compatibility with custom indicators.
        """
        return 0
