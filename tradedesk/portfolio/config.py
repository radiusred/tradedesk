from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PortfolioConfig:
    instruments: list[str]
    output_dir: Path
    default_risk_per_trade: float
    portfolio_risk_budget: float
    atr_period: int
    atr_risk_mult: float
    min_size: float
    max_size: float
    base_period: str | None


@dataclass(frozen=True)
class BacktestPortfolioConfig(PortfolioConfig):
    input_dir: Path
    half_spread_adjustment: float

    @classmethod
    def from_raw(
        cls,
        *,
        instruments: list[str],
        input_dir: Path,
        output_dir: Path,
        half_spread_adjustment: float,
        risk: dict[str, Any],
        sizing: dict[str, Any],
        base_period: str | None,
    ) -> BacktestPortfolioConfig:
        """Validate and construct from raw config dicts.

        Raises ``ValueError`` with a clear message on bad/missing values
        instead of letting ``KeyError`` or ``TypeError`` propagate.
        """
        try:
            default_risk = float(risk["default_risk_per_trade"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "portfolio.risk.default_risk_per_trade is missing or not numeric"
            ) from exc
        try:
            budget = float(risk["portfolio_risk_budget"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "portfolio.risk.portfolio_risk_budget is missing or not numeric"
            ) from exc

        return cls(
            instruments=instruments,
            input_dir=input_dir,
            output_dir=output_dir,
            half_spread_adjustment=float(half_spread_adjustment),
            default_risk_per_trade=default_risk,
            portfolio_risk_budget=budget,
            atr_period=int(sizing.get("atr_period", 14)),
            atr_risk_mult=float(sizing.get("atr_risk_mult", 1.0)),
            min_size=float(sizing.get("min_size", 0.1)),
            max_size=float(sizing.get("max_size", 5.0)),
            base_period=base_period,
        )


@dataclass(frozen=True)
class LivePortfolioConfig(PortfolioConfig):
    period: str
    reconcile_interval: int = 4
    margin_check_enabled: bool = True
    journal_enabled: bool = True
