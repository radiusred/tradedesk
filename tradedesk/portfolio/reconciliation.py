"""Position reconciliation between local journal and broker state."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from tradedesk.recording import JournalEntry, PositionJournal

from ..events import DomainEvent
from ..execution import BrokerPosition
from ..types import Direction
from .runner import PortfolioRunner
from .types import Instrument, ReconcilableStrategy

log = logging.getLogger(__name__)


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
        return [
            e for e in self.entries if e.discrepancy == DiscrepancyType.ORPHAN_BROKER
        ]

    @property
    def phantom_local_positions(self) -> list[ReconciliationEntry]:
        return [
            e for e in self.entries if e.discrepancy == DiscrepancyType.PHANTOM_LOCAL
        ]


def _direction_matches(journal_dir: str | None, broker_dir: str) -> bool:
    """Compare journal direction (long/short) to broker direction (BUY/SELL)."""
    if journal_dir is None:
        return False
    mapping = {"long": "BUY", "short": "SELL"}
    return mapping.get(journal_dir, "") == broker_dir


def reconcile(
    *,
    journal_positions: dict[str, JournalEntry],
    broker_positions: list[BrokerPosition],
    managed_instruments: set[str],
) -> ReconciliationResult:
    """
    Compare local journal state against broker positions.

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
    entries: list[ReconciliationEntry] = []

    # Index broker positions by instrument (only managed ones)
    broker_by_instrument: dict[str, BrokerPosition] = {}
    for bp in broker_positions:
        if bp.instrument in managed_instruments:
            broker_by_instrument[bp.instrument] = bp

    all_instruments = managed_instruments | set(broker_by_instrument.keys())

    for instrument in sorted(all_instruments):
        journal_entry = journal_positions.get(instrument)
        broker_pos = broker_by_instrument.get(instrument)

        journal_has_position = (
            journal_entry is not None and journal_entry.direction is not None
        )
        broker_has_position = broker_pos is not None

        if not journal_has_position and not broker_has_position:
            # Both flat
            entries.append(
                ReconciliationEntry(
                    instrument=instrument,
                    discrepancy=DiscrepancyType.MATCHED,
                )
            )

        elif journal_has_position and broker_has_position:
            assert journal_entry is not None and broker_pos is not None
            # Both have a position -- check details
            if not _direction_matches(journal_entry.direction, broker_pos.direction):
                entries.append(
                    ReconciliationEntry(
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
                )
            elif abs((journal_entry.size or 0) - broker_pos.size) > 1e-6:
                entries.append(
                    ReconciliationEntry(
                        instrument=instrument,
                        discrepancy=DiscrepancyType.SIZE_MISMATCH,
                        journal_direction=journal_entry.direction,
                        journal_size=journal_entry.size,
                        broker_direction=broker_pos.direction,
                        broker_size=broker_pos.size,
                        broker_deal_id=broker_pos.deal_id,
                        message=(
                            f"Size mismatch: journal={journal_entry.size} "
                            f"broker={broker_pos.size}"
                        ),
                    )
                )
            else:
                entries.append(
                    ReconciliationEntry(
                        instrument=instrument,
                        discrepancy=DiscrepancyType.MATCHED,
                        journal_direction=journal_entry.direction,
                        journal_size=journal_entry.size,
                        broker_direction=broker_pos.direction,
                        broker_size=broker_pos.size,
                        broker_deal_id=broker_pos.deal_id,
                    )
                )

        elif not journal_has_position and broker_has_position:
            assert broker_pos is not None
            # Journal says flat but broker has position.
            # If journal entry exists with direction=None, we TRIED to close
            # but broker still has it => FAILED_EXIT.
            if journal_entry is not None and journal_entry.direction is None:
                entries.append(
                    ReconciliationEntry(
                        instrument=instrument,
                        discrepancy=DiscrepancyType.FAILED_EXIT,
                        broker_direction=broker_pos.direction,
                        broker_size=broker_pos.size,
                        broker_deal_id=broker_pos.deal_id,
                        message=(
                            "EMERGENCY: Journal records flat but broker has position "
                            "(failed exit?)"
                        ),
                    )
                )
            else:
                entries.append(
                    ReconciliationEntry(
                        instrument=instrument,
                        discrepancy=DiscrepancyType.ORPHAN_BROKER,
                        broker_direction=broker_pos.direction,
                        broker_size=broker_pos.size,
                        broker_deal_id=broker_pos.deal_id,
                        message="Broker has position not tracked in journal",
                    )
                )

        else:
            # journal_has_position and not broker_has_position
            assert journal_entry is not None
            entries.append(
                ReconciliationEntry(
                    instrument=instrument,
                    discrepancy=DiscrepancyType.PHANTOM_LOCAL,
                    journal_direction=journal_entry.direction,
                    journal_size=journal_entry.size,
                    message=(
                        "Journal has position but broker does not "
                        "(was it closed externally?)"
                    ),
                )
            )

    return ReconciliationResult(entries=entries)


class ReconciliationManager:
    """Manages position reconciliation, journal persistence, and margin logging.

    Broker is source of truth. Journal is a crash-recovery mechanism.

    Strategies must implement the ``ReconcilableStrategy`` protocol
    (``to_journal_entry``, ``restore_from_journal``, ``check_restored_position``)
    for journal persistence and post-reconciliation exit checks.
    """

    def __init__(
        self,
        *,
        runner: PortfolioRunner,
        client: Any,
        journal: PositionJournal | None,
        target_period: str,
        reconcile_interval: int = 4,
        enable_event_subscription: bool = True,
    ):
        self._runner = runner
        self.client = client
        self._journal = journal
        self._target_period = target_period
        self._reconcile_interval = reconcile_interval
        self._candle_count: int = 0
        self._recently_changed_instruments: set[str] = set()
        self._enable_event_subscription = enable_event_subscription

        # Self-subscribe to events if enabled
        if enable_event_subscription:
            from tradedesk.events import get_dispatcher
            from tradedesk.marketdata.events import CandleClosedEvent

            dispatcher = get_dispatcher()
            dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
            log.debug(
                "ReconciliationManager subscribed to CandleClosedEvent (target_period=%s)",
                target_period,
            )

    async def _on_candle_closed(self, event: DomainEvent) -> None:
        """Handle target-period candle events for periodic reconciliation."""
        from tradedesk.marketdata.events import CandleClosedEvent

        if (
            not isinstance(event, CandleClosedEvent)
            or event.timeframe != self._target_period
        ):
            return

        # Increment candle counter
        self._candle_count += 1

        # Check if reconciliation needed
        if self._should_reconcile_now():
            await self.periodic_reconcile()
            await self.log_margin_status()

    def _should_reconcile_now(self) -> bool:
        """Internal check if reconciliation threshold reached (without incrementing)."""
        return (
            self._journal is not None
            and self._candle_count % self._reconcile_interval == 0
        )

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    async def reconcile_on_startup(self) -> set[str]:
        """Load journal, compare with broker, resolve discrepancies.

        Returns the set of instruments that had positions restored/adopted
        (and therefore need a post-warmup exit check).
        """
        if self._journal is None:
            return set()

        restored_instruments: set[str] = set()

        journal_entries = self._journal.load()
        journal_positions: dict[str, JournalEntry] = {}
        if journal_entries is not None:
            for j_entry in journal_entries:
                journal_positions[j_entry.instrument] = j_entry
            open_count = sum(1 for e in journal_entries if e.direction is not None)
            log.info(
                "Journal loaded: %d entries (%d open, %d flat)",
                len(journal_entries),
                open_count,
                len(journal_entries) - open_count,
            )
        else:
            log.info("No journal found; starting fresh")

        # Fetch broker state
        try:
            broker_positions = await self.client.get_positions()
        except Exception:
            log.exception(
                "Failed to fetch broker positions for reconciliation; "
                "restoring from journal only"
            )
            if journal_entries is not None:
                restored_instruments = self._restore_from_journal(journal_positions)
            return restored_instruments

        managed_instruments = {str(inst) for inst in self._runner.strategies.keys()}

        result = reconcile(
            journal_positions=journal_positions,
            broker_positions=broker_positions,
            managed_instruments=managed_instruments,
        )

        # Log reconciliation summary
        if result.is_clean:
            log.info(
                "Startup reconciliation: all %d positions match (broker has %d open positions)",
                len(result.entries),
                len(broker_positions),
            )
        else:
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
                    log.warning(
                        "Reconciliation: %s -- %s", entry.instrument, entry.message
                    )

        # Handle emergencies
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

        # Handle phantoms
        for entry in result.phantom_local_positions:
            log.warning(
                "Phantom position cleared: %s (journal said %s size=%s, broker has nothing)",
                entry.instrument,
                entry.journal_direction,
                entry.journal_size,
            )

        # Handle orphan broker positions: adopt into local state
        for entry in result.orphan_broker_positions:
            log.warning(
                "Adopting orphan broker position: %s %s size=%s deal=%s",
                entry.instrument,
                entry.broker_direction,
                entry.broker_size,
                entry.broker_deal_id,
            )

        # Restore reconciled state
        restored_instruments = self._apply_reconciliation(
            result, journal_positions, broker_positions
        )

        # Persist corrected state so journal matches reality
        if not result.is_clean:
            self.persist_positions()

        return restored_instruments

    def _restore_from_journal(
        self, journal_positions: dict[str, JournalEntry]
    ) -> set[str]:
        """Restore positions from journal only (broker unreachable)."""
        restored: set[str] = set()
        for inst, s in self._runner.strategies.items():
            strat = cast(ReconcilableStrategy, s)
            epic = str(inst)
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

    def _apply_reconciliation(
        self,
        result: ReconciliationResult,
        journal_positions: dict[str, JournalEntry],
        broker_positions: list[BrokerPosition],
    ) -> set[str]:
        """Apply reconciliation decisions and restore positions into strategies.

        Returns set of instruments with restored positions that need re-evaluation.
        """
        broker_by_instrument = {bp.instrument: bp for bp in broker_positions}
        restored: set[str] = set()

        for inst, s in self._runner.strategies.items():
            strat = cast(ReconcilableStrategy, s)
            epic = str(inst)
            entry = next((e for e in result.entries if e.instrument == epic), None)
            if entry is None:
                continue

            if (
                entry.discrepancy == DiscrepancyType.MATCHED
                and entry.journal_direction is not None
            ):
                # Restore from journal (has bars_held, mfe_points, entry_atr)
                journal_entry = journal_positions.get(epic)
                if journal_entry is not None:
                    strat.restore_from_journal(journal_entry)
                    log.info(
                        "Restored matched position: %s %s size=%s",
                        epic,
                        journal_entry.direction,
                        journal_entry.size,
                    )
                    restored.add(epic)

            elif entry.discrepancy == DiscrepancyType.ORPHAN_BROKER:
                # Adopt broker position
                direction = (
                    Direction.LONG
                    if entry.broker_direction == "BUY"
                    else Direction.SHORT
                )
                broker_pos = broker_by_instrument.get(epic)
                entry_price = broker_pos.entry_price if broker_pos else 0.0
                strat.position.open(direction, entry.broker_size or 0.0, entry_price)
                log.info(
                    "Adopted orphan: %s %s size=%s entry=%s",
                    epic,
                    entry.broker_direction,
                    entry.broker_size,
                    entry_price,
                )
                restored.add(epic)

            elif entry.discrepancy == DiscrepancyType.SIZE_MISMATCH:
                # Trust broker size, keep journal direction/metadata
                journal_entry = journal_positions.get(epic)
                if journal_entry is not None and entry.broker_size is not None:
                    strat.restore_from_journal(journal_entry)
                    strat.position.size = entry.broker_size
                    log.warning(
                        "Restored with broker size: %s journal_size=%s broker_size=%s",
                        epic,
                        journal_entry.size,
                        entry.broker_size,
                    )
                    restored.add(epic)

            elif entry.discrepancy in (
                DiscrepancyType.FAILED_EXIT,
                DiscrepancyType.DIRECTION_MISMATCH,
            ):
                # Broker has a position that contradicts journal -- adopt broker state
                direction = (
                    Direction.LONG
                    if entry.broker_direction == "BUY"
                    else Direction.SHORT
                )
                broker_pos = broker_by_instrument.get(epic)
                entry_price = broker_pos.entry_price if broker_pos else 0.0
                strat.position.open(direction, entry.broker_size or 0.0, entry_price)
                log.warning(
                    "Adopted broker position: %s %s size=%s entry=%s (was: %s)",
                    epic,
                    entry.broker_direction,
                    entry.broker_size,
                    entry_price,
                    entry.discrepancy.value,
                )
                restored.add(epic)

            # PHANTOM_LOCAL: leave strategy flat (broker has nothing)

        return restored

    # ------------------------------------------------------------------
    # Post-reconciliation exit checks
    # ------------------------------------------------------------------

    async def post_warmup_check(self, restored_instruments: set[str]) -> None:
        """Evaluate exit conditions on restored/adopted positions.

        After warmup primes indicators, any position that has been restored
        or adopted might be stale (e.g. stop-loss breached, regime deactivated).
        This method checks each one and exits immediately if warranted.
        """
        for inst, s in self._runner.strategies.items():
            strat = cast(ReconcilableStrategy, s)
            epic = str(inst)
            if epic not in restored_instruments:
                continue
            if strat.position.is_flat():
                continue

            log.info(
                "Post-reconciliation check for %s: position %s size=%s - checking exit conditions",
                epic,
                strat.position.direction.value if strat.position.direction else "?",
                strat.position.size,
            )

            # Fetch latest candle to get current price
            try:
                candles = await self.client.get_historical_candles(
                    epic, self._target_period, 1
                )
                if not candles:
                    log.warning(
                        "No candles available for post-reconciliation check on %s", epic
                    )
                    continue

                candle = candles[-1]
                await strat.check_restored_position(candle)

                if strat.position.is_flat():
                    log.info(
                        "Post-reconciliation: %s position was closed (exit condition met)",
                        epic,
                    )
                else:
                    log.info("Post-reconciliation: %s position retained", epic)
            except Exception:
                log.exception("Post-reconciliation check failed for %s", epic)

    # ------------------------------------------------------------------
    # Position journal persistence
    # ------------------------------------------------------------------

    def persist_positions(self, changed_epic: str = "") -> None:
        """Save current position state of all strategies to journal."""
        if changed_epic:
            self._recently_changed_instruments.add(changed_epic)
        if self._journal is None:
            return

        entries = []
        for inst, s in self._runner.strategies.items():
            strat = cast(ReconcilableStrategy, s)
            entries.append(strat.to_journal_entry(str(inst)))

        self._journal.save(entries)

    # ------------------------------------------------------------------
    # Periodic reconciliation
    # ------------------------------------------------------------------

    async def periodic_reconcile(self) -> None:
        """Periodic check: sync local state to match broker (source of truth)."""
        try:
            broker_positions = await self.client.get_positions()
        except Exception:
            log.warning(
                "Periodic reconciliation skipped: failed to fetch broker positions"
            )
            return

        # Build current local state
        journal_positions: dict[str, JournalEntry] = {}
        for inst, s in self._runner.strategies.items():
            strat = cast(ReconcilableStrategy, s)
            epic = str(inst)
            journal_positions[epic] = strat.to_journal_entry(epic)

        managed_instruments = {str(inst) for inst in self._runner.strategies.keys()}

        # Exclude instruments with recent position changes to avoid settlement race
        skipped = self._recently_changed_instruments.copy()
        self._recently_changed_instruments.clear()
        if skipped:
            log.debug(
                "Periodic reconciliation: skipping recently-changed instruments: %s",
                skipped,
            )
            managed_instruments -= skipped

        result = reconcile(
            journal_positions=journal_positions,
            broker_positions=broker_positions,
            managed_instruments=managed_instruments,
        )

        if result.is_clean:
            log.debug("Periodic reconciliation: all positions match")
            return

        broker_by_instrument = {bp.instrument: bp for bp in broker_positions}
        corrected = False
        adopted_instruments: set[str] = set()

        for entry in result.entries:
            if entry.discrepancy == DiscrepancyType.MATCHED:
                continue

            maybe_strat = self._runner.strategies.get(Instrument(entry.instrument))
            if maybe_strat is None:
                continue
            strat = cast(ReconcilableStrategy, maybe_strat)

            if entry.discrepancy == DiscrepancyType.PHANTOM_LOCAL:
                # Broker has no position -- reset local to flat
                log.warning(
                    "PHANTOM corrected: %s was %s size=%s locally but broker has no position; "
                        "resetting to flat",
                    entry.instrument,
                    entry.journal_direction,
                    entry.journal_size,
                )
                strat.position.reset()
                corrected = True

            elif entry.discrepancy in (
                DiscrepancyType.FAILED_EXIT,
                DiscrepancyType.ORPHAN_BROKER,
            ):
                # Broker has position we don't have locally -- adopt it
                direction = (
                    Direction.LONG
                    if entry.broker_direction == "BUY"
                    else Direction.SHORT
                )
                bp = broker_by_instrument.get(entry.instrument)
                entry_price = bp.entry_price if bp else 0.0
                strat.position.open(direction, entry.broker_size or 0.0, entry_price)
                log.warning(
                    "Adopted broker position: %s %s size=%s entry=%.4f (was: %s)",
                    entry.instrument,
                    entry.broker_direction,
                    entry.broker_size,
                    entry_price,
                    entry.discrepancy.value,
                )
                adopted_instruments.add(entry.instrument)
                corrected = True

            elif entry.discrepancy == DiscrepancyType.SIZE_MISMATCH:
                # Trust broker size
                log.warning(
                    "Size corrected: %s local=%s broker=%s; adopting broker size",
                    entry.instrument,
                    entry.journal_size,
                    entry.broker_size,
                )
                strat.position.size = entry.broker_size
                corrected = True

            elif entry.discrepancy == DiscrepancyType.DIRECTION_MISMATCH:
                # Broker direction wins -- reset and re-open with broker state
                direction = (
                    Direction.LONG
                    if entry.broker_direction == "BUY"
                    else Direction.SHORT
                )
                bp = broker_by_instrument.get(entry.instrument)
                entry_price = bp.entry_price if bp else 0.0
                strat.position.reset()
                strat.position.open(direction, entry.broker_size or 0.0, entry_price)
                log.warning(
                    "Direction corrected: %s local=%s broker=%s; adopting broker state",
                    entry.instrument,
                    entry.journal_direction,
                    entry.broker_direction,
                )
                adopted_instruments.add(entry.instrument)
                corrected = True

        if corrected:
            self.persist_positions()

        # Evaluate exit conditions on any newly adopted positions
        if adopted_instruments:
            await self.post_warmup_check(adopted_instruments)

    # ------------------------------------------------------------------
    # Periodic check gate
    # ------------------------------------------------------------------

    def should_reconcile(self) -> bool:
        """Return True if enough candles have passed for a periodic check.

        Note: If event subscription is disabled (backward compat mode), this
        method increments the counter. If event subscription is enabled, the
        counter is managed by the event handler.
        """
        # Only increment counter if not using event subscription (backward compat)
        if not self._enable_event_subscription:
            self._candle_count += 1

        return self._should_reconcile_now()

    # ------------------------------------------------------------------
    # Margin logging
    # ------------------------------------------------------------------

    async def log_margin_status(self) -> None:
        """Log current margin utilisation."""
        get_balance = getattr(self.client, "get_account_balance", None)
        if not callable(get_balance):
            return
        try:
            balance = await get_balance()
            utilisation = (
                (balance.deposit / balance.balance * 100) if balance.balance > 0 else 0
            )
            log.info(
                "Margin status: balance=%.2f deposit=%.2f available=%.2f utilisation=%.1f%%",
                balance.balance,
                balance.deposit,
                balance.available,
                utilisation,
            )
        except Exception:
            log.debug("Failed to fetch margin status")
