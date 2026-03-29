"""Portfolio orchestration."""

from dataclasses import dataclass

from tradedesk.marketdata import CandleClosedEvent

from .risk import RiskAllocationPolicy
from .types import Instrument, PortfolioStrategy, SleeveId


@dataclass
class PortfolioRunner:
    """
    Client-agnostic portfolio orchestrator.

    Responsibilities:
      - Maintain a set of per-sleeve strategies, keyed by SleeveId
      - Compute active set k from strategy state (previous close)
      - Apply risk policy before processing the next candle close
      - Forward candle events to all strategies on the relevant instrument

    Does NOT:
      - Place orders (strategies + their clients do that)
      - Perform portfolio rebalancing
      - Attempt to increase utilisation

    Two strategies on the same instrument (e.g. AdaptiveFade_AUDCAD and
    BollingerReversion_AUDCAD) are stored under distinct SleeveIds. When a
    candle arrives for AUDCAD, both sleeves receive the event.
    """

    strategies: dict[SleeveId, PortfolioStrategy]
    policy: RiskAllocationPolicy
    default_risk_per_trade: float

    def _active_sleeve_instruments(self) -> dict[SleeveId, Instrument]:
        """Return mapping of active SleeveId -> underlying Instrument."""
        return {
            sid: s.instrument
            for sid, s in self.strategies.items()
            if s.is_regime_active()
        }

    def _apply_risk_budgets(self) -> None:
        """Apply risk allocation policy to all strategies."""
        active = self._active_sleeve_instruments()
        alloc = self.policy.allocate(active)

        # If no regimes active, revert to default risk for all strategies.
        if not alloc:
            for s in self.strategies.values():
                s.set_risk_per_trade(float(self.default_risk_per_trade))
            return

        # Active sleeves get allocated risk; inactive sleeves get default.
        for sid, s in self.strategies.items():
            if sid in alloc:
                s.set_risk_per_trade(float(alloc[sid]))
            else:
                s.set_risk_per_trade(float(self.default_risk_per_trade))

    async def on_candle_close(self, event: CandleClosedEvent) -> None:
        """
        Process a candle close event using two-phase lifecycle.

        All strategy sleeves whose instrument matches the event receive the
        event. This supports multiple sleeves on the same instrument (e.g.
        dual-strategy AUDCAD trading).

        Phase 1: Update state for all matching sleeves
        Phase 2: Apply risk budgets across the whole portfolio
        Phase 3: Evaluate signals for all matching sleeves

        Args:
            event: Candle close event with instrument, period, and candle data
        """
        matching = [
            s for s in self.strategies.values()
            if s.instrument == Instrument(event.instrument)
        ]
        if not matching:
            return

        # Phase 1: Update state for all matching sleeves
        for strat in matching:
            await strat.update_state(event)

        # Phase 2: Apply risk budgets based on updated regime state (portfolio-wide)
        self._apply_risk_budgets()

        # Phase 3: Evaluate signals for all matching sleeves
        for strat in matching:
            await strat.evaluate_signals()
