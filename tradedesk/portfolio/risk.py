"""Risk management utilities."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from .types import Instrument


def atr_normalised_size(
    *,
    risk_per_trade: float,
    atr: float,
    atr_risk_mult: float,
    min_size: float,
    max_size: float,
) -> float:
    """
    Calculate position size normalized by ATR.

    Position size is calculated as: risk_per_trade / (atr * atr_risk_mult)
    Result is clamped between min_size and max_size.

    Args:
        risk_per_trade: Amount of capital to risk per trade
        atr: Current ATR value
        atr_risk_mult: ATR multiplier for stop distance
        min_size: Minimum position size
        max_size: Maximum position size

    Returns:
        Position size clamped to [min_size, max_size]
    """
    denom = float(atr) * float(atr_risk_mult)
    if denom <= 0.0:
        return float(min_size)
    raw = float(risk_per_trade) / denom
    return max(float(min_size), min(float(max_size), raw))


class RiskAllocationPolicy(ABC):
    """
    Base class for portfolio risk allocation policies.

    Risk allocation policies determine how to distribute a portfolio's risk budget
    across multiple instruments based on regime activity or other criteria.
    """

    @abstractmethod
    def allocate(self, active_instruments: list[Instrument]) -> Mapping[Instrument, float]:
        """
        Allocate risk budget across active instruments.

        Args:
            active_instruments: List of instruments to allocate risk to

        Returns:
            Mapping of instrument to allocated risk amount (typically used as risk_per_trade)
        """
        pass


@dataclass(frozen=True)
class EqualSplitRiskPolicy(RiskAllocationPolicy):
    """
    Split a fixed portfolio risk budget across concurrently active regimes.

    Semantics:
      - If k active regimes: allocate budget/k to each active instrument.
      - If k == 0: allocate nothing (caller decides what to do when no regimes active).
    """

    portfolio_risk_budget: float

    def allocate(self, active_instruments: list[Instrument]) -> Mapping[Instrument, float]:
        """
        Allocate risk budget across active instruments.

        Args:
            active_instruments: List of instruments with active regimes

        Returns:
            Mapping of instrument to allocated risk amount
        """
        if not active_instruments:
            return {}
        k = len(active_instruments)
        per = float(self.portfolio_risk_budget) / float(k)
        return {inst: per for inst in active_instruments}
