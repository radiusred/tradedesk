"""Risk management utilities."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping

from .types import Instrument, SleeveId


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
    across multiple strategy sleeves based on regime activity or other criteria.

    Allocation is keyed by ``SleeveId`` so that two strategies on the same
    instrument (e.g. ``AdaptiveFade_AUDCAD`` and ``BollingerReversion_AUDCAD``)
    receive independent risk budgets.
    """

    @abstractmethod
    def allocate(self, active_sleeves: Mapping[SleeveId, Instrument]) -> Mapping[SleeveId, float]:
        """
        Allocate risk budget across active strategy sleeves.

        Args:
            active_sleeves: Mapping of SleeveId to its underlying Instrument for
                strategies whose regime is currently active.  Passing the instrument
                allows policies to look up per-instrument history even when sleeve
                names differ from raw instrument symbols.

        Returns:
            Mapping of SleeveId to allocated risk amount (used as risk_per_trade).
        """
        pass


@dataclass(frozen=True)
class EqualSplitRiskPolicy(RiskAllocationPolicy):
    """
    Split a fixed portfolio risk budget equally across concurrently active sleeves.

    Semantics:
      - If k active sleeves: allocate budget/k to each active sleeve.
      - If k == 0: allocate nothing (caller falls back to default_risk_per_trade).
    """

    portfolio_risk_budget: float

    def allocate(self, active_sleeves: Mapping[SleeveId, Instrument]) -> Mapping[SleeveId, float]:
        """
        Allocate risk budget across active strategy sleeves.

        Args:
            active_sleeves: Mapping of SleeveId to its underlying Instrument.

        Returns:
            Mapping of SleeveId to allocated risk amount.
        """
        if not active_sleeves:
            return {}
        k = len(active_sleeves)
        per = float(self.portfolio_risk_budget) / float(k)
        return {sleeve: per for sleeve in active_sleeves}
