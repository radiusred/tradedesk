"""Pure reconciliation types and the comparison algorithm.

This module is side-effect free: it computes a :class:`ReconciliationResult`
from journal entries and broker positions. All policy decisions about how to
react to a discrepancy live in :mod:`.policy`.
"""

from dataclasses import dataclass
from enum import Enum

from ...execution import BrokerPosition
from ..journal import JournalEntry


class DiscrepancyType(Enum):
    """Classification of journal-vs-broker mismatches."""

    MATCHED = "matched"
    ORPHAN_BROKER = "orphan_broker"  # broker has position, journal does not
    PHANTOM_LOCAL = "phantom_local"  # journal has position, broker does not
    SIZE_MISMATCH = "size_mismatch"  # both have it, sizes differ
    DIRECTION_MISMATCH = "direction_mismatch"  # both have it, directions differ
    FAILED_EXIT = "failed_exit"  # journal says flat, broker still has position


@dataclass(frozen=True)
class ReconciliationEntry:
    """Result of comparing a single instrument."""

    instrument: str
    discrepancy: DiscrepancyType
    journal_direction: str | None = None
    journal_size: float | None = None
    broker_direction: str | None = None
    broker_size: float | None = None
    broker_deal_id: str | None = None
    message: str = ""


@dataclass
class ReconciliationResult:
    """Complete result of reconciliation across all instruments."""

    entries: list[ReconciliationEntry]

    @property
    def is_clean(self) -> bool:
        return all(e.discrepancy == DiscrepancyType.MATCHED for e in self.entries)

    @property
    def has_emergencies(self) -> bool:
        return any(e.discrepancy == DiscrepancyType.FAILED_EXIT for e in self.entries)

    @property
    def orphan_broker_positions(self) -> list[ReconciliationEntry]:
        return [e for e in self.entries if e.discrepancy == DiscrepancyType.ORPHAN_BROKER]

    @property
    def phantom_local_positions(self) -> list[ReconciliationEntry]:
        return [e for e in self.entries if e.discrepancy == DiscrepancyType.PHANTOM_LOCAL]


def _direction_matches(journal_dir: str | None, broker_dir: str) -> bool:
    """Compare journal direction (long/short) to broker direction (BUY/SELL)."""
    if journal_dir is None:
        return False
    mapping = {"long": "BUY", "short": "SELL"}
    return mapping.get(journal_dir, "") == broker_dir


def _classify_both_present(
    instrument: str, journal_entry: JournalEntry, broker_pos: BrokerPosition
) -> ReconciliationEntry:
    """Classify a pair where both journal and broker hold a position."""
    if not _direction_matches(journal_entry.direction, broker_pos.direction):
        return ReconciliationEntry(
            instrument=instrument,
            discrepancy=DiscrepancyType.DIRECTION_MISMATCH,
            journal_direction=journal_entry.direction,
            journal_size=journal_entry.size,
            broker_direction=broker_pos.direction,
            broker_size=broker_pos.size,
            broker_deal_id=broker_pos.deal_id,
            message=(
                f"Direction mismatch: journal={journal_entry.direction} "
                f"broker={broker_pos.direction}"
            ),
        )
    if abs((journal_entry.size or 0) - broker_pos.size) > 1e-6:
        return ReconciliationEntry(
            instrument=instrument,
            discrepancy=DiscrepancyType.SIZE_MISMATCH,
            journal_direction=journal_entry.direction,
            journal_size=journal_entry.size,
            broker_direction=broker_pos.direction,
            broker_size=broker_pos.size,
            broker_deal_id=broker_pos.deal_id,
            message=f"Size mismatch: journal={journal_entry.size} broker={broker_pos.size}",
        )
    return ReconciliationEntry(
        instrument=instrument,
        discrepancy=DiscrepancyType.MATCHED,
        journal_direction=journal_entry.direction,
        journal_size=journal_entry.size,
        broker_direction=broker_pos.direction,
        broker_size=broker_pos.size,
        broker_deal_id=broker_pos.deal_id,
    )


def _classify_broker_only(
    instrument: str, journal_entry: JournalEntry | None, broker_pos: BrokerPosition
) -> ReconciliationEntry:
    """Classify a pair where the journal is flat/absent but the broker has a position.

    A journal entry with ``direction=None`` means we previously tried to close,
    so a broker position now is a FAILED_EXIT (real emergency). No journal entry
    at all means the broker position is simply unknown to us (orphan).
    """
    if journal_entry is not None and journal_entry.direction is None:
        return ReconciliationEntry(
            instrument=instrument,
            discrepancy=DiscrepancyType.FAILED_EXIT,
            broker_direction=broker_pos.direction,
            broker_size=broker_pos.size,
            broker_deal_id=broker_pos.deal_id,
            message="EMERGENCY: Journal records flat but broker has position (failed exit?)",
        )
    return ReconciliationEntry(
        instrument=instrument,
        discrepancy=DiscrepancyType.ORPHAN_BROKER,
        broker_direction=broker_pos.direction,
        broker_size=broker_pos.size,
        broker_deal_id=broker_pos.deal_id,
        message="Broker has position not tracked in journal",
    )


def _classify_pair(
    instrument: str,
    journal_entry: JournalEntry | None,
    broker_pos: BrokerPosition | None,
) -> ReconciliationEntry:
    """Classify a single instrument's journal-vs-broker state."""
    journal_has_position = journal_entry is not None and journal_entry.direction is not None
    broker_has_position = broker_pos is not None

    if not journal_has_position and not broker_has_position:
        return ReconciliationEntry(instrument=instrument, discrepancy=DiscrepancyType.MATCHED)

    if journal_has_position and broker_has_position:
        assert journal_entry is not None and broker_pos is not None
        return _classify_both_present(instrument, journal_entry, broker_pos)

    if broker_has_position:
        assert broker_pos is not None
        return _classify_broker_only(instrument, journal_entry, broker_pos)

    assert journal_entry is not None
    return ReconciliationEntry(
        instrument=instrument,
        discrepancy=DiscrepancyType.PHANTOM_LOCAL,
        journal_direction=journal_entry.direction,
        journal_size=journal_entry.size,
        message="Journal has position but broker does not (was it closed externally?)",
    )


def reconcile(
    *,
    journal_positions: dict[str, JournalEntry],
    broker_positions: list[BrokerPosition],
    managed_instruments: set[str],
) -> ReconciliationResult:
    """Compare local journal state against broker positions.

    Args:
        journal_positions: Position state from the on-disk journal, keyed by
            instrument.  Entries with ``direction=None`` represent flat (no position).
        broker_positions: Live positions from ``GET /positions``.
        managed_instruments: The set of instruments this portfolio instance manages.
            Broker positions for instruments NOT in this set are ignored (they may
            belong to manual trades or a different portfolio).

    Returns:
        A :class:`ReconciliationResult` with per-instrument entries.
    """
    broker_by_instrument: dict[str, BrokerPosition] = {
        bp.instrument: bp for bp in broker_positions if bp.instrument in managed_instruments
    }
    all_instruments = managed_instruments | set(broker_by_instrument.keys())

    entries = [
        _classify_pair(
            instrument,
            journal_positions.get(instrument),
            broker_by_instrument.get(instrument),
        )
        for instrument in sorted(all_instruments)
    ]
    return ReconciliationResult(entries=entries)
