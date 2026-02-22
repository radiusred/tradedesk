"""Core types and protocols for portfolio-level strategy management.

This module defines the data structures and interfaces used by the
`PortfolioRunner` to orchestrate multiple strategies across different
instruments.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, NewType, Protocol

if TYPE_CHECKING:
    from ..execution import PositionTracker
    from ..marketdata import CandleClosedEvent
    from ..types import Candle
    from .journal import JournalEntry

Instrument = NewType("Instrument", str)


@dataclass(frozen=True)
class StrategySpec:
    """Specification for a single strategy instance within a portfolio.

    This dataclass binds a strategy class to a specific instrument and period,
    along with its configuration. The `PortfolioRunner` uses a list of these
    specs to construct the portfolio.

    Attributes:
        instrument: The instrument identifier (e.g., an IG epic).
        period: The chart timeframe for the strategy (e.g., "15MINUTE").
        strategy_cls: The strategy class to instantiate.
        kwargs: A dictionary of keyword arguments to pass to the strategy's
            `__init__` method during instantiation.
    """

    instrument: str
    period: str
    strategy_cls: Callable[..., Any]
    kwargs: dict[str, Any]


class PortfolioStrategy(Protocol):
    """Protocol defining the interface for strategies managed by `PortfolioRunner`.

    A class implementing this protocol can be managed as part of a larger
    portfolio, receiving risk budget updates and candle events from an
    orchestrator.

    Strategies should implement the two-phase lifecycle:
    1. update_state() - Update indicators, regime state, position tracking
    2. evaluate_signals() - Make entry/exit decisions with correct risk allocations
    """

    instrument: Instrument

    def set_risk_per_trade(self, value: float) -> None:
        """Set the strategy's per-trade risk budget.

        This is called by the `PortfolioRunner` after state updates and before
        signal evaluation, ensuring trading decisions use current risk allocations.

        Args:
            value: The monetary amount to risk on the next trade.
        """
        ...

    def is_regime_active(self) -> bool:
        """Check if the strategy's underlying trading regime is currently active.

        The `PortfolioRunner` uses this to determine the set of "active"
        strategies for risk allocation purposes.

        Returns:
            True if the regime is active, False otherwise.
        """
        ...

    async def update_state(self, event: "CandleClosedEvent") -> None:
        """Update indicators and regime state based on new candle.

        This phase happens before risk allocation. Strategies should update
        their internal state (indicators, regime filters, position tracking)
        but NOT make entry/exit decisions.

        Args:
            event: The `CandleClosedEvent` containing the completed candle.
        """
        ...

    async def evaluate_signals(self) -> None:
        """Evaluate entry/exit signals and execute trades.

        This phase happens after risk allocation. Strategies should check
        their current state and make trading decisions using the allocated
        risk budget (set via set_risk_per_trade).

        At this point, all indicators and regime state reflect the latest
        candle, and risk_per_trade reflects the current portfolio allocation.
        """
        ...


class ReconcilableStrategy(Protocol):
    """Protocol for strategies that support position reconciliation.

    Implementations handle their own serialisation and exit logic so that
    ``ReconciliationManager`` stays strategy-agnostic.
    """

    position: "PositionTracker"

    def to_journal_entry(self, instrument: str) -> "JournalEntry":
        """Serialise position and strategy-specific state to a journal entry."""
        ...

    def restore_from_journal(self, entry: "JournalEntry") -> None:
        """Restore position and strategy-specific state from a journal entry."""
        ...

    async def check_restored_position(self, candle: "Candle") -> None:
        """Evaluate exit conditions on a restored or adopted position.

        Called after warmup on positions restored from journal or adopted
        from broker.  The strategy should check stop-loss, take-profit,
        regime-off etc. and exit if warranted.
        """
        ...
