"""Reconciliation manager: event subscription glue and orchestration.

The manager wires up domain events to reconciliation policy operations and
owns side-effecting concerns (broker calls, journal persistence, candle
counters). All decision logic lives in :mod:`.policy`.
"""

import asyncio
import logging
from typing import Any, cast

from ...events import DomainEvent, SessionReadyEvent, SessionStartedEvent, get_dispatcher
from ...execution import OrderCompletedEvent
from ...marketdata import CandleClosedEvent
from ..journal import JournalEntry, PositionJournal
from ..runner import PortfolioRunner
from ..types import ReconcilableStrategy
from .metrics import RECONCILIATION_FAILURES
from .policy import (
    apply_periodic_corrections,
    apply_startup_decisions,
    log_startup_summary,
    restore_from_journal_only,
)
from .result import reconcile

log = logging.getLogger(__name__)

# Broker-call failure modes we expect to recover from (network blips, HTTP
# errors, rate-limit RuntimeErrors raised by IGClient._request, async
# timeouts). Anything outside this set is a programming error and must
# propagate so it is surfaced in monitoring.
_BROKER_CALL_ERRORS: tuple[type[BaseException], ...] = (
    RuntimeError,
    OSError,
    asyncio.TimeoutError,
)


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
        self._restored_instruments: set[str] = set()

        if enable_event_subscription:
            self._subscribe_events()

    def _subscribe_events(self) -> None:
        dispatcher = get_dispatcher()
        dispatcher.subscribe(SessionStartedEvent, self._on_session_started)
        dispatcher.subscribe(SessionReadyEvent, self._on_session_ready)
        dispatcher.subscribe(CandleClosedEvent, self._on_candle_closed)
        dispatcher.subscribe(OrderCompletedEvent, self._on_order_completed)
        log.debug(
            "ReconciliationManager subscribed to session and candle events (target_period=%s)",
            self._target_period,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_session_started(self, event: SessionStartedEvent) -> None:
        """Run startup reconciliation when the portfolio session begins."""
        self._restored_instruments = await self.reconcile_on_startup()

    async def _on_session_ready(self, event: SessionReadyEvent) -> None:
        """Run post-warmup checks once warmup and reconciliation are complete."""
        if self._restored_instruments:
            await self.post_warmup_check(self._restored_instruments)

    async def _on_candle_closed(self, event: DomainEvent) -> None:
        """Handle target-period candle events for periodic reconciliation."""
        if not isinstance(event, CandleClosedEvent) or event.timeframe != self._target_period:
            return
        self._candle_count += 1
        if self._should_reconcile_now():
            await self.periodic_reconcile()

    async def _on_order_completed(self, _event: OrderCompletedEvent) -> None:
        await self.log_margin_status()

    # ------------------------------------------------------------------
    # Periodic check gate
    # ------------------------------------------------------------------

    def _should_reconcile_now(self) -> bool:
        """Internal check if reconciliation threshold reached (without incrementing)."""
        return self._journal is not None and self._candle_count % self._reconcile_interval == 0

    def should_reconcile(self) -> bool:
        """Return True if enough candles have passed for a periodic check.

        Note: If event subscription is disabled (backward compat mode), this
        method increments the counter. If event subscription is enabled, the
        counter is managed by the event handler.
        """
        if not self._enable_event_subscription:
            self._candle_count += 1
        return self._should_reconcile_now()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _strategies_by_instrument(self) -> dict[str, ReconcilableStrategy]:
        return {
            str(s.instrument): cast(ReconcilableStrategy, s)
            for s in self._runner.strategies.values()
        }

    def _managed_instruments(self) -> set[str]:
        return {str(s.instrument) for s in self._runner.strategies.values()}

    def _take_recently_changed(self) -> set[str]:
        """Snapshot and clear the set of recently changed instruments."""
        skipped = self._recently_changed_instruments.copy()
        self._recently_changed_instruments.clear()
        return skipped

    @staticmethod
    def _index_journal_entries(
        entries: list[JournalEntry] | None,
    ) -> dict[str, JournalEntry]:
        if entries is None:
            return {}
        return {e.instrument: e for e in entries}

    def _snapshot_local_state(self) -> dict[str, JournalEntry]:
        """Serialise current strategy state into journal-entry form."""
        return {
            str(s.instrument): cast(ReconcilableStrategy, s).to_journal_entry(str(s.instrument))
            for s in self._runner.strategies.values()
        }

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

        journal_entries = self._journal.load()
        journal_positions = self._index_journal_entries(journal_entries)
        if journal_entries is not None:
            open_count = sum(1 for e in journal_entries if e.direction is not None)
            log.info(
                "Journal loaded: %d entries (%d open, %d flat)",
                len(journal_entries),
                open_count,
                len(journal_entries) - open_count,
            )
        else:
            log.info("No journal found; starting fresh")

        try:
            broker_positions = await self.client.get_positions()
        except _BROKER_CALL_ERRORS:
            RECONCILIATION_FAILURES.labels(operation="startup_get_positions").inc()
            log.exception(
                "Failed to fetch broker positions for reconciliation; restoring from journal only"
            )
            if journal_entries is None:
                return set()
            return restore_from_journal_only(
                self._strategies_by_instrument(), journal_positions
            )

        strategies_by_instrument = self._strategies_by_instrument()
        result = reconcile(
            journal_positions=journal_positions,
            broker_positions=broker_positions,
            managed_instruments=set(strategies_by_instrument),
        )
        log_startup_summary(result, len(broker_positions))

        restored = apply_startup_decisions(
            strategies_by_instrument, result, journal_positions, broker_positions
        )

        if not result.is_clean:
            self.persist_positions()

        return restored

    # ------------------------------------------------------------------
    # Periodic reconciliation
    # ------------------------------------------------------------------

    async def periodic_reconcile(self) -> None:
        """Periodic check: sync local state to match broker (source of truth)."""
        try:
            broker_positions = await self.client.get_positions()
        except _BROKER_CALL_ERRORS:
            RECONCILIATION_FAILURES.labels(operation="periodic_get_positions").inc()
            log.warning(
                "Periodic reconciliation skipped: failed to fetch broker positions",
                exc_info=True,
            )
            return

        journal_positions = self._snapshot_local_state()
        strategies_by_instrument = self._strategies_by_instrument()
        managed_instruments = set(strategies_by_instrument)

        skipped = self._take_recently_changed()
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

        corrected, adopted_instruments = apply_periodic_corrections(
            strategies_by_instrument, result, broker_positions
        )

        if corrected:
            self.persist_positions()

        if adopted_instruments:
            await self.post_warmup_check(adopted_instruments)

    # ------------------------------------------------------------------
    # Post-reconciliation exit checks
    # ------------------------------------------------------------------

    async def post_warmup_check(self, restored_instruments: set[str]) -> None:
        """Evaluate exit conditions on restored/adopted positions.

        After warmup primes indicators, any position that has been restored
        or adopted might be stale (e.g. stop-loss breached, regime deactivated).
        This method checks each one and exits immediately if warranted.
        """
        for s in self._runner.strategies.values():
            strat = cast(ReconcilableStrategy, s)
            epic = str(s.instrument)
            if epic not in restored_instruments or strat.position.is_flat():
                continue
            await self._check_one_restored(strat, epic)

    async def _check_one_restored(self, strat: ReconcilableStrategy, epic: str) -> None:
        log.info(
            "Post-reconciliation check for %s: position %s size=%s - checking exit conditions",
            epic,
            strat.position.direction.value if strat.position.direction else "?",
            strat.position.size,
        )
        try:
            candles = await self.client.get_historical_candles(epic, self._target_period, 1)
            if not candles:
                log.warning("No candles available for post-reconciliation check on %s", epic)
                return

            await strat.check_restored_position(candles[-1])

            if strat.position.is_flat():
                log.info("Post-reconciliation: %s position was closed (exit condition met)", epic)
            else:
                log.info("Post-reconciliation: %s position retained", epic)
        except _BROKER_CALL_ERRORS:
            RECONCILIATION_FAILURES.labels(operation="post_warmup_check").inc()
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

        entries = [
            cast(ReconcilableStrategy, s).to_journal_entry(str(s.instrument))
            for s in self._runner.strategies.values()
        ]
        self._journal.save(entries)

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
            utilisation = (balance.deposit / balance.balance * 100) if balance.balance > 0 else 0
            log.info(
                "Margin status: balance=%.2f deposit=%.2f available=%.2f utilisation=%.1f%%",
                balance.balance,
                balance.deposit,
                balance.available,
                utilisation,
            )
        except _BROKER_CALL_ERRORS:
            RECONCILIATION_FAILURES.labels(operation="margin_status").inc()
            log.warning("Failed to fetch margin status", exc_info=True)
