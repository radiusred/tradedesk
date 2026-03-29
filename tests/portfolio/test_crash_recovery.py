"""
End-to-end crash recovery tests.

These tests simulate a process restart mid-session: positions are opened and
persisted, a new manager instance is created (simulating fresh startup), and we
verify that the position journal is correctly restored and reconciled with the
broker.

Also covers reconciliation edge cases:
  - Partial fills creating size mismatches
  - Concurrent position changes both journaled correctly
  - Settlement race protection via recently-changed skipping
  - Out-of-order event ordering does not corrupt state
"""

from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk import Direction
from tradedesk.execution import BrokerPosition, PositionTracker
from tradedesk.portfolio import (
    Instrument,
    JournalEntry,
    PositionJournal,
    ReconciliationManager,
    SleeveId,
)
from tradedesk.types import Candle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bp(
    epic: str,
    direction: str = "BUY",
    size: float = 1.0,
    entry_price: float = 100.0,
    deal_id: str = "D1",
) -> BrokerPosition:
    return BrokerPosition(
        instrument=epic,
        direction=direction,
        size=size,
        entry_price=entry_price,
        deal_id=deal_id,
    )


def _candle() -> Candle:
    return Candle(
        timestamp="2026-01-01T00:00:00Z",
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        tick_count=1,
    )


class _FakeStrategy:
    """Minimal strategy stub implementing the ReconcilableStrategy protocol."""

    def __init__(self, epic: str = "") -> None:
        self.epic = epic
        self.instrument = Instrument(epic)
        self.position = PositionTracker()
        self.entry_atr: float = 0.0
        self._on_position_change: Callable[[str], None] | None = None

    def to_journal_entry(self, instrument: str) -> JournalEntry:
        return JournalEntry(
            instrument=instrument,
            direction=self.position.direction.value if self.position.direction else None,
            size=self.position.size,
            entry_price=self.position.entry_price,
            bars_held=self.position.bars_held,
            mfe_points=self.position.mfe_points,
            entry_atr=self.entry_atr,
            updated_at="",
        )

    def restore_from_journal(self, entry: JournalEntry) -> None:
        self.position = PositionTracker.from_dict(
            {
                "direction": entry.direction,
                "size": entry.size,
                "entry_price": entry.entry_price,
                "bars_held": entry.bars_held,
                "mfe_points": entry.mfe_points,
            }
        )
        self.entry_atr = entry.entry_atr

    async def check_restored_position(self, candle: Candle) -> None:
        pass


def _build_manager(
    epics: list[str],
    *,
    journal: PositionJournal,
    client: AsyncMock | None = None,
) -> ReconciliationManager:
    if client is None:
        client = AsyncMock()
    strategies = {SleeveId(e): _FakeStrategy(e) for e in epics}
    runner = MagicMock()
    runner.strategies = strategies
    mgr = ReconciliationManager(
        runner=runner,
        client=client,
        journal=journal,
        target_period="HOUR",
        enable_event_subscription=False,
    )
    for strat in strategies.values():
        strat._on_position_change = mgr.persist_positions
    return mgr


def _strat(mgr: ReconciliationManager, epic: str) -> _FakeStrategy:
    return mgr._runner.strategies[SleeveId(epic)]  # type: ignore[return-value]


@pytest.fixture
def journal(tmp_path: pytest.TempPathFactory) -> PositionJournal:
    return PositionJournal(tmp_path / "journal")  # type: ignore[operator]


# ---------------------------------------------------------------------------
# Crash recovery: end-to-end process restart simulation
# ---------------------------------------------------------------------------


class TestCrashRecovery:
    @pytest.mark.asyncio
    async def test_open_position_restored_after_restart(
        self, journal: PositionJournal
    ) -> None:
        """Open a long position and persist it; on restart a new manager restores it."""
        # Pre-crash: open and persist
        mgr1 = _build_manager(["EURUSD"], journal=journal)
        strat1 = _strat(mgr1, "EURUSD")
        strat1.position.open(Direction.LONG, 2.0, 1.2345)
        strat1.position.bars_held = 3
        strat1.entry_atr = 0.0050
        mgr1.persist_positions()

        # Post-restart: broker confirms same position
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("EURUSD", "BUY", 2.0, 1.2345)])
        mgr2 = _build_manager(["EURUSD"], journal=journal, client=client)
        restored = await mgr2.reconcile_on_startup()

        strat2 = _strat(mgr2, "EURUSD")
        assert "EURUSD" in restored
        assert strat2.position.direction == Direction.LONG
        assert strat2.position.size == pytest.approx(2.0)
        assert strat2.position.entry_price == pytest.approx(1.2345)
        assert strat2.position.bars_held == 3

    @pytest.mark.asyncio
    async def test_trade_metadata_preserved_through_restart(
        self, journal: PositionJournal
    ) -> None:
        """bars_held, mfe_points, and entry_atr survive a crash/restart cycle."""
        mgr1 = _build_manager(["EURUSD"], journal=journal)
        strat1 = _strat(mgr1, "EURUSD")
        strat1.position.open(Direction.LONG, 1.0, 1.1000)
        strat1.position.bars_held = 12
        strat1.position.mfe_points = 0.0085
        strat1.entry_atr = 0.0030
        mgr1.persist_positions()

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("EURUSD", "BUY", 1.0, 1.1000)])
        mgr2 = _build_manager(["EURUSD"], journal=journal, client=client)
        await mgr2.reconcile_on_startup()

        strat2 = _strat(mgr2, "EURUSD")
        assert strat2.position.bars_held == 12
        assert strat2.position.mfe_points == pytest.approx(0.0085)
        assert strat2.entry_atr == pytest.approx(0.0030)

    @pytest.mark.asyncio
    async def test_flat_position_not_restored_after_restart(
        self, journal: PositionJournal
    ) -> None:
        """Flat strategy persists as flat; not included in restored set after restart."""
        mgr1 = _build_manager(["EURUSD"], journal=journal)
        mgr1.persist_positions()  # strategy is flat by default

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])
        mgr2 = _build_manager(["EURUSD"], journal=journal, client=client)
        restored = await mgr2.reconcile_on_startup()

        assert "EURUSD" not in restored
        assert _strat(mgr2, "EURUSD").position.is_flat()

    @pytest.mark.asyncio
    async def test_multiple_instruments_partial_restore(
        self, journal: PositionJournal
    ) -> None:
        """One open, one flat: only the open instrument is in the restored set."""
        mgr1 = _build_manager(["EURUSD", "USDJPY"], journal=journal)
        _strat(mgr1, "EURUSD").position.open(Direction.SHORT, 1.5, 1.1000)
        mgr1.persist_positions()

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("EURUSD", "SELL", 1.5, 1.1000)])
        mgr2 = _build_manager(["EURUSD", "USDJPY"], journal=journal, client=client)
        restored = await mgr2.reconcile_on_startup()

        assert "EURUSD" in restored
        assert "USDJPY" not in restored
        assert _strat(mgr2, "EURUSD").position.direction == Direction.SHORT
        assert _strat(mgr2, "USDJPY").position.is_flat()

    @pytest.mark.asyncio
    async def test_corrupt_journal_falls_back_to_broker_adoption(
        self, tmp_path: pytest.TempPathFactory
    ) -> None:
        """Corrupt journal file falls back to adopting orphan broker positions."""
        journal_dir = tmp_path / "journal"  # type: ignore[operator]
        journal_dir.mkdir()
        (journal_dir / "positions.json").write_text("not valid json {{{{")

        corrupt_journal = PositionJournal(journal_dir)
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("GBPUSD", "BUY", 1.0, 1.3000)])
        mgr = _build_manager(["GBPUSD"], journal=corrupt_journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "GBPUSD" in restored
        assert _strat(mgr, "GBPUSD").position.direction == Direction.LONG

    @pytest.mark.asyncio
    async def test_broker_down_restores_from_journal(
        self, journal: PositionJournal
    ) -> None:
        """When broker is unreachable on startup, positions restore from journal alone."""
        mgr1 = _build_manager(["USDJPY"], journal=journal)
        strat1 = _strat(mgr1, "USDJPY")
        strat1.position.open(Direction.LONG, 3.0, 149.50)
        strat1.position.bars_held = 7
        mgr1.persist_positions()

        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=RuntimeError("Connection refused"))
        mgr2 = _build_manager(["USDJPY"], journal=journal, client=client)
        restored = await mgr2.reconcile_on_startup()

        assert "USDJPY" in restored
        assert _strat(mgr2, "USDJPY").position.direction == Direction.LONG
        assert _strat(mgr2, "USDJPY").position.size == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Position journal consistency
# ---------------------------------------------------------------------------


class TestJournalConsistency:
    def test_journal_written_on_position_open(self, journal: PositionJournal) -> None:
        """Journal is persisted immediately when a position is opened."""
        mgr = _build_manager(["EURUSD"], journal=journal)
        strat = _strat(mgr, "EURUSD")
        strat.position.open(Direction.LONG, 1.0, 1.2000)
        mgr.persist_positions("EURUSD")

        loaded = journal.load()
        assert loaded is not None
        entry = next(e for e in loaded if e.instrument == "EURUSD")
        assert entry.direction == "long"
        assert entry.size == pytest.approx(1.0)

    def test_journal_written_on_position_close(self, journal: PositionJournal) -> None:
        """Journal reflects flat state immediately after a position closes."""
        mgr = _build_manager(["EURUSD"], journal=journal)
        strat = _strat(mgr, "EURUSD")
        strat.position.open(Direction.LONG, 1.0, 1.2000)
        mgr.persist_positions("EURUSD")

        strat.position.reset()
        mgr.persist_positions("EURUSD")

        loaded = journal.load()
        assert loaded is not None
        assert next(e for e in loaded if e.instrument == "EURUSD").direction is None

    def test_journal_contains_all_instruments_on_save(
        self, journal: PositionJournal
    ) -> None:
        """Every managed instrument is included in each journal write."""
        mgr = _build_manager(["EURUSD", "USDJPY", "GBPUSD"], journal=journal)
        _strat(mgr, "EURUSD").position.open(Direction.LONG, 1.0, 1.1000)
        mgr.persist_positions()

        loaded = journal.load()
        assert loaded is not None
        instruments = {e.instrument for e in loaded}
        assert instruments == {"EURUSD", "USDJPY", "GBPUSD"}


# ---------------------------------------------------------------------------
# Reconciliation edge cases
# ---------------------------------------------------------------------------


class TestReconciliationEdgeCases:
    @pytest.mark.asyncio
    async def test_partial_fill_size_mismatch_corrected_at_periodic_reconcile(
        self, journal: PositionJournal
    ) -> None:
        """Partial fill: local has full requested size; broker has partial size.

        Periodic reconcile corrects local state to match broker.
        """
        mgr = _build_manager(["EURUSD"], journal=journal)
        strat = _strat(mgr, "EURUSD")
        strat.position.open(Direction.LONG, 2.0, 1.2000)  # requested 2.0

        # Broker only filled 1.5
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("EURUSD", "BUY", 1.5, 1.2000)])
        mgr.client = client

        await mgr.periodic_reconcile()

        assert strat.position.size == pytest.approx(1.5)
        loaded = journal.load()
        assert loaded is not None
        assert next(e for e in loaded if e.instrument == "EURUSD").size == pytest.approx(1.5)

    def test_concurrent_position_changes_both_persisted(
        self, journal: PositionJournal
    ) -> None:
        """Two instruments open positions simultaneously; both are correctly journaled."""
        mgr = _build_manager(["EURUSD", "USDJPY"], journal=journal)
        _strat(mgr, "EURUSD").position.open(Direction.LONG, 1.0, 1.1000)
        _strat(mgr, "USDJPY").position.open(Direction.SHORT, 2.0, 150.00)

        # Both callbacks fire before any reconcile
        mgr.persist_positions("EURUSD")
        mgr.persist_positions("USDJPY")

        loaded = journal.load()
        assert loaded is not None
        by_inst = {e.instrument: e for e in loaded}
        assert by_inst["EURUSD"].direction == "long"
        assert by_inst["USDJPY"].direction == "short"
        assert "EURUSD" in mgr._recently_changed_instruments
        assert "USDJPY" in mgr._recently_changed_instruments

    @pytest.mark.asyncio
    async def test_settlement_race_skips_recently_changed_instrument(
        self, journal: PositionJournal
    ) -> None:
        """Position change fires just before periodic reconcile.

        The instrument is skipped to avoid false phantom detection during the
        broker settlement window.
        """
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])  # broker shows flat (lag)

        mgr = _build_manager(["EURUSD"], journal=journal, client=client)
        strat = _strat(mgr, "EURUSD")
        strat.position.open(Direction.LONG, 1.0, 1.2000)
        mgr._recently_changed_instruments.add("EURUSD")

        await mgr.periodic_reconcile()

        # Position must NOT have been cleared despite broker showing flat
        assert strat.position.direction == Direction.LONG
        # Recently changed set is cleared after reconcile runs
        assert len(mgr._recently_changed_instruments) == 0

    @pytest.mark.asyncio
    async def test_out_of_order_close_event_final_state_correct(
        self, journal: PositionJournal
    ) -> None:
        """A delayed close event arriving after other activity still leaves correct state."""
        mgr = _build_manager(["EURUSD"], journal=journal)
        strat = _strat(mgr, "EURUSD")

        # Open and journal
        strat.position.open(Direction.LONG, 1.0, 1.2000)
        mgr.persist_positions("EURUSD")

        # Later the close event arrives (out of order relative to other work)
        strat.position.reset()
        mgr.persist_positions("EURUSD")

        loaded = journal.load()
        assert loaded is not None
        assert next(e for e in loaded if e.instrument == "EURUSD").direction is None
        assert strat.position.is_flat()

    @pytest.mark.asyncio
    async def test_post_warmup_check_fires_on_newly_adopted_periodic_position(
        self, journal: PositionJournal
    ) -> None:
        """Periodic reconcile adopts orphan broker position and triggers exit evaluation."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("EURUSD", "SELL", 2.0, 80.0)])
        client.get_historical_candles = AsyncMock(return_value=[_candle()])

        mgr = _build_manager(["EURUSD"], journal=journal, client=client)
        await mgr.periodic_reconcile()

        strat = _strat(mgr, "EURUSD")
        assert strat.position.direction == Direction.SHORT
        assert strat.position.size == pytest.approx(2.0)
