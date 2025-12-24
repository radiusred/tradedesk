"""Base class for technical indicators."""

import abc

from tradedesk.chartdata import Candle


class Indicator(abc.ABC):
    """Abstract base class for all technical indicators."""

    @abc.abstractmethod
    def update(self, candle: Candle):
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

