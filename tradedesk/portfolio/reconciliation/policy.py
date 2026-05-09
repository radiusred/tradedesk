"""Reconciliation policy: how to apply decisions to strategies.

These functions take the immutable :class:`ReconciliationResult` and the
relevant strategy/broker context, then mutate strategy state to match the
broker (the source of truth). Logging happens here too — the manager just
orchestrates events and persistence.
"""

import logging

from ...execution import BrokerPosition
from ...types import Direction
from ..journal import JournalEntry
from ..types import ReconcilableStrategy
from .result import (
    DiscrepancyType,
    ReconciliationEntry,
    ReconciliationResult,
)

log = logging.getLogger(__name__)


def _estimate_entry_atr(strat: ReconcilableStrategy) -> float:
    """Best-effort ATR estimate from the strategy's regime for adopted positions."""
    regime = getattr(strat, "regime", None)
    if regime is not None:
        atr = getattr(regime, "atr_value", None)
        if atr is not None:
            return float(atr)
    return 0.0


def _adopt_broker_position(
    strat: ReconcilableStrategy,
    broker_direction: str | None,
    broker_size: float | None,
    entry_price: float,
) -> None:
    """Open a strategy position to match the broker's state."""
    direction = Direction.LONG if broker_direction == "BUY" else Direction.SHORT
    strat.position.open(direction, broker_size or 0.0, entry_price)
    strat.entry_atr = _estimate_entry_atr(strat)


def _broker_entry_price(
    broker_by_instrument: dict[str, BrokerPosition], instrument: str
) -> float:
    bp = broker_by_instrument.get(instrument)
    return bp.entry_price if bp else 0.0


def restore_from_journal_only(
    strategies_by_instrument: dict[str, ReconcilableStrategy],
    journal_positions: dict[str, JournalEntry],
) -> set[str]:
    """Restore strategy positions from the journal when the broker is unreachable."""
    restored: set[str] = set()
    for epic, strat in strategies_by_instrument.items():
        entry = journal_positions.get(epic)
        if entry is None or entry.direction is None:
            continue
        strat.restore_from_journal(entry)
        log.info(
            "Restored position from journal: %s %s size=%s entry=%s",
            epic,
            entry.direction,
            entry.size,
            entry.entry_price,
        )
        restored.add(epic)
    return restored


def log_startup_summary(result: ReconciliationResult, broker_count: int) -> None:
    """Log the high-level outcome of a startup reconciliation."""
    if result.is_clean:
        log.info(
            "Startup reconciliation: all %d positions match (broker has %d open positions)",
            len(result.entries),
            broker_count,
        )
        return

    for entry in result.entries:
        if entry.discrepancy == DiscrepancyType.MATCHED:
            if entry.journal_direction is not None:
                log.info(
                    "Reconciliation OK: %s %s size=%s",
                    entry.instrument,
                    entry.journal_direction,
                    entry.journal_size,
                )
        else:
            log.warning("Reconciliation: %s -- %s", entry.instrument, entry.message)

    if result.has_emergencies:
        for e in result.entries:
            if e.discrepancy == DiscrepancyType.FAILED_EXIT:
                log.critical(
                    "FAILED EXIT DETECTED: %s has broker position %s size=%s "
                    "deal=%s but journal says flat. Manual intervention required.",
                    e.instrument,
                    e.broker_direction,
                    e.broker_size,
                    e.broker_deal_id,
                )

    for entry in result.phantom_local_positions:
        log.warning(
            "Phantom position cleared: %s (journal said %s size=%s, broker has nothing)",
            entry.instrument,
            entry.journal_direction,
            entry.journal_size,
        )

    for entry in result.orphan_broker_positions:
        log.warning(
            "Adopting orphan broker position: %s %s size=%s deal=%s",
            entry.instrument,
            entry.broker_direction,
            entry.broker_size,
            entry.broker_deal_id,
        )


def _apply_startup_entry(
    strat: ReconcilableStrategy,
    entry: ReconciliationEntry,
    journal_positions: dict[str, JournalEntry],
    broker_by_instrument: dict[str, BrokerPosition],
) -> bool:
    """Apply a single startup decision; return True if the position was restored/adopted."""
    epic = entry.instrument

    if entry.discrepancy == DiscrepancyType.MATCHED and entry.journal_direction is not None:
        journal_entry = journal_positions.get(epic)
        if journal_entry is None:
            return False
        strat.restore_from_journal(journal_entry)
        log.info(
            "Restored matched position: %s %s size=%s",
            epic,
            journal_entry.direction,
            journal_entry.size,
        )
        return True

    if entry.discrepancy == DiscrepancyType.ORPHAN_BROKER:
        entry_price = _broker_entry_price(broker_by_instrument, epic)
        _adopt_broker_position(strat, entry.broker_direction, entry.broker_size, entry_price)
        log.info(
            "Adopted orphan: %s %s size=%s entry=%s atr=%s",
            epic,
            entry.broker_direction,
            entry.broker_size,
            entry_price,
            strat.entry_atr,
        )
        return True

    if entry.discrepancy == DiscrepancyType.SIZE_MISMATCH:
        journal_entry = journal_positions.get(epic)
        if journal_entry is None or entry.broker_size is None:
            return False
        strat.restore_from_journal(journal_entry)
        strat.position.size = entry.broker_size
        log.warning(
            "Restored with broker size: %s journal_size=%s broker_size=%s",
            epic,
            journal_entry.size,
            entry.broker_size,
        )
        return True

    if entry.discrepancy in (DiscrepancyType.FAILED_EXIT, DiscrepancyType.DIRECTION_MISMATCH):
        entry_price = _broker_entry_price(broker_by_instrument, epic)
        _adopt_broker_position(strat, entry.broker_direction, entry.broker_size, entry_price)
        log.warning(
            "Adopted broker position: %s %s size=%s entry=%s atr=%s (was: %s)",
            epic,
            entry.broker_direction,
            entry.broker_size,
            entry_price,
            strat.entry_atr,
            entry.discrepancy.value,
        )
        return True

    # PHANTOM_LOCAL: leave strategy flat (broker has nothing)
    return False


def apply_startup_decisions(
    strategies_by_instrument: dict[str, ReconcilableStrategy],
    result: ReconciliationResult,
    journal_positions: dict[str, JournalEntry],
    broker_positions: list[BrokerPosition],
) -> set[str]:
    """Restore strategies based on startup reconciliation decisions.

    Returns the set of instruments whose positions were restored or adopted
    and therefore need a post-warmup exit check.
    """
    broker_by_instrument = {bp.instrument: bp for bp in broker_positions}
    entries_by_instrument = {e.instrument: e for e in result.entries}
    restored: set[str] = set()

    for epic, strat in strategies_by_instrument.items():
        entry = entries_by_instrument.get(epic)
        if entry is None:
            continue
        if _apply_startup_entry(strat, entry, journal_positions, broker_by_instrument):
            restored.add(epic)

    return restored


def _apply_periodic_entry(
    strat: ReconcilableStrategy,
    entry: ReconciliationEntry,
    broker_by_instrument: dict[str, BrokerPosition],
    adopted_instruments: set[str],
) -> bool:
    """Apply a single periodic correction; return True if any state changed."""
    if entry.discrepancy == DiscrepancyType.PHANTOM_LOCAL:
        log.warning(
            "PHANTOM corrected: %s was %s size=%s locally but broker has no position; "
            "resetting to flat",
            entry.instrument,
            entry.journal_direction,
            entry.journal_size,
        )
        strat.position.reset()
        return True

    if entry.discrepancy in (DiscrepancyType.FAILED_EXIT, DiscrepancyType.ORPHAN_BROKER):
        entry_price = _broker_entry_price(broker_by_instrument, entry.instrument)
        _adopt_broker_position(strat, entry.broker_direction, entry.broker_size, entry_price)
        log.warning(
            "Adopted broker position: %s %s size=%s entry=%.4f atr=%s (was: %s)",
            entry.instrument,
            entry.broker_direction,
            entry.broker_size,
            entry_price,
            strat.entry_atr,
            entry.discrepancy.value,
        )
        adopted_instruments.add(entry.instrument)
        return True

    if entry.discrepancy == DiscrepancyType.SIZE_MISMATCH:
        log.warning(
            "Size corrected: %s local=%s broker=%s; adopting broker size",
            entry.instrument,
            entry.journal_size,
            entry.broker_size,
        )
        strat.position.size = entry.broker_size
        return True

    if entry.discrepancy == DiscrepancyType.DIRECTION_MISMATCH:
        entry_price = _broker_entry_price(broker_by_instrument, entry.instrument)
        strat.position.reset()
        _adopt_broker_position(strat, entry.broker_direction, entry.broker_size, entry_price)
        log.warning(
            "Direction corrected: %s local=%s broker=%s; adopting broker state (atr=%s)",
            entry.instrument,
            entry.journal_direction,
            entry.broker_direction,
            strat.entry_atr,
        )
        adopted_instruments.add(entry.instrument)
        return True

    return False


def apply_periodic_corrections(
    strategies_by_instrument: dict[str, ReconcilableStrategy],
    result: ReconciliationResult,
    broker_positions: list[BrokerPosition],
) -> tuple[bool, set[str]]:
    """Correct strategies during a periodic reconciliation.

    Returns ``(corrected, adopted_instruments)``: ``corrected`` is True when
    any state changed (so the journal needs persisting); ``adopted_instruments``
    are the freshly opened positions that need a post-warmup exit check.
    """
    broker_by_instrument = {bp.instrument: bp for bp in broker_positions}
    corrected = False
    adopted_instruments: set[str] = set()

    for entry in result.entries:
        if entry.discrepancy == DiscrepancyType.MATCHED:
            continue
        strat = strategies_by_instrument.get(entry.instrument)
        if strat is None:
            continue
        if _apply_periodic_entry(strat, entry, broker_by_instrument, adopted_instruments):
            corrected = True

    return corrected, adopted_instruments
