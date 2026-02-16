"""Tests for ReconciliationManager: startup reconciliation, periodic correction, and journal persistence."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from tradedesk.execution.position import PositionTracker
from tradedesk.execution import Direction
from tradedesk.execution import BrokerPosition
from tradedesk.portfolio import Instrument
from tradedesk.types import Candle

from tradedesk.recording.journal import PositionJournal, JournalEntry
from tradedesk.portfolio.reconciliation import ReconciliationManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _je(
    epic,
    direction=None,
    size=None,
    entry_price=None,
    bars_held=0,
    mfe_points=0.0,
    entry_atr=0.0,
):
    return JournalEntry(
        instrument=epic,
        direction=direction,
        size=size,
        entry_price=entry_price,
        bars_held=bars_held,
        mfe_points=mfe_points,
        entry_atr=entry_atr,
        updated_at="",
    )


def _bp(epic, direction="BUY", size=1.0, entry_price=100.0, deal_id="D1"):
    return BrokerPosition(
        instrument=epic,
        direction=direction,
        size=size,
        entry_price=entry_price,
        deal_id=deal_id,
    )


def _candle():
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
    """Minimal strategy for reconciliation tests."""

    def __init__(self, client=None, epic="", period="", **kwargs):
        self.client = client
        self.epic = epic
        self.position = PositionTracker()
        self._on_position_change = None
        self.entry_atr = 0.0
        self._check_exits_called = False

    def is_regime_active(self):
        return True

    def warmup_enabled(self):
        return False

    async def warmup(self):
        pass

    async def _check_exits(self, c, regime_active):
        self._check_exits_called = True

    def notify_position_change(self):
        if self._on_position_change is not None:
            self._on_position_change(self.epic)

    # -- ReconcilableStrategy protocol --

    def to_journal_entry(self, instrument):
        return JournalEntry(
            instrument=instrument,
            direction=self.position.direction.value
            if self.position.direction
            else None,
            size=self.position.size,
            entry_price=self.position.entry_price,
            bars_held=self.position.bars_held,
            mfe_points=self.position.mfe_points,
            entry_atr=self.entry_atr,
            updated_at="",
        )

    def restore_from_journal(self, entry):
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

    async def check_restored_position(self, candle):
        await self._check_exits(candle, self.is_regime_active())


def _build_manager(epics, *, journal, client=None):
    """Build a ReconciliationManager with fake strategies."""
    client = client or AsyncMock()

    strategies = {}
    for epic in epics:
        strat = _FakeStrategy(client=client, epic=epic)
        strategies[Instrument(epic)] = strat

    runner = MagicMock()
    runner.strategies = strategies

    mgr = ReconciliationManager(
        runner=runner,
        client=client,
        journal=journal,
        target_period="HOUR",
    )

    # Register position-change callbacks (mirrors real orchestrator wiring)
    for inst, strat in strategies.items():
        strat._on_position_change = mgr.persist_positions

    return mgr


def _strat(mgr, epic):
    """Get strategy by epic string."""
    return mgr._runner.strategies[Instrument(epic)]


@pytest.fixture
def journal(tmp_path):
    return PositionJournal(tmp_path / "journal")


# ---------------------------------------------------------------------------
# Startup reconciliation: reconcile_on_startup
# ---------------------------------------------------------------------------


class TestStartupReconciliation:
    @pytest.mark.asyncio
    async def test_matched_position_restored(self, journal):
        """Matching journal + broker position restores from journal."""
        journal.save([_je("A", "long", 1.0, 100.0, bars_held=5, mfe_points=2.5)])

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "BUY", 1.0, 100.0)])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" in restored
        pos = _strat(mgr, "A").position
        assert pos.direction == Direction.LONG
        assert pos.size == 1.0
        assert pos.bars_held == 5
        assert pos.mfe_points == 2.5

    @pytest.mark.asyncio
    async def test_phantom_local_cleared(self, journal):
        """Journal has position, broker doesn't -> strategy flat, journal updated."""
        journal.save([_je("A", "long", 1.0, 100.0)])

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" not in restored
        assert _strat(mgr, "A").position.is_flat()

        loaded = journal.load()
        assert loaded[0].direction is None

    @pytest.mark.asyncio
    async def test_orphan_broker_adopted(self, journal):
        """Broker has position with no journal -> adopted into strategy."""
        # No journal saved (fresh start)
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "SELL", 2.0, 50.0)])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" in restored
        pos = _strat(mgr, "A").position
        assert pos.direction == Direction.SHORT
        assert pos.size == 2.0
        assert pos.entry_price == 50.0

    @pytest.mark.asyncio
    async def test_failed_exit_adopted(self, journal):
        """Journal flat, broker has position -> adopt broker state."""
        journal.save([_je("A", None, None)])

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "BUY", 3.0, 120.0)])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" in restored
        pos = _strat(mgr, "A").position
        assert pos.direction == Direction.LONG
        assert pos.size == 3.0

    @pytest.mark.asyncio
    async def test_direction_mismatch_adopts_broker(self, journal):
        """Journal says long, broker says SELL -> adopt broker direction."""
        journal.save([_je("A", "long", 1.0, 100.0)])

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "SELL", 1.5, 95.0)])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" in restored
        pos = _strat(mgr, "A").position
        assert pos.direction == Direction.SHORT
        assert pos.size == 1.5

        # Journal persisted with corrected state
        loaded = journal.load()
        entry = next(e for e in loaded if e.instrument == "A")
        assert entry.direction == "short"

    @pytest.mark.asyncio
    async def test_size_mismatch_trusts_broker(self, journal):
        """Same direction, different size -> adopt broker size."""
        journal.save([_je("A", "long", 1.0, 100.0)])

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "BUY", 2.5, 100.0)])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" in restored
        assert _strat(mgr, "A").position.size == 2.5

    @pytest.mark.asyncio
    async def test_journal_persisted_after_correction(self, journal):
        """Non-clean reconciliation persists corrected state to journal."""
        journal.save([_je("A", "long", 1.0, 100.0)])

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])  # phantom

        mgr = _build_manager(["A"], journal=journal, client=client)
        await mgr.reconcile_on_startup()

        loaded = journal.load()
        assert loaded is not None
        assert loaded[0].direction is None

    @pytest.mark.asyncio
    async def test_clean_reconciliation_skips_persist(self, journal):
        """When everything matches, journal is not re-written."""
        journal.save([_je("A")])  # flat

        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])

        mgr = _build_manager(["A"], journal=journal, client=client)

        # Spy on persist_positions
        mgr.persist_positions = MagicMock()
        await mgr.reconcile_on_startup()
        mgr.persist_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_journal_starts_fresh(self, journal):
        """No journal file -> logs fresh start, no crash."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert restored == set()
        assert _strat(mgr, "A").position.is_flat()

    @pytest.mark.asyncio
    async def test_broker_unreachable_restores_from_journal(self, journal):
        """When broker API fails, positions restored from journal alone."""
        journal.save([_je("A", "long", 1.0, 100.0)])

        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=RuntimeError("HTTP 500"))

        mgr = _build_manager(["A"], journal=journal, client=client)
        restored = await mgr.reconcile_on_startup()

        assert "A" in restored
        pos = _strat(mgr, "A").position
        assert pos.direction == Direction.LONG
        assert pos.size == 1.0


# ---------------------------------------------------------------------------
# Periodic reconciliation: periodic_reconcile
# ---------------------------------------------------------------------------


class TestPeriodicReconciliation:
    @pytest.mark.asyncio
    async def test_all_clean_no_changes(self, journal):
        """When everything matches, no corrections are made."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])

        mgr = _build_manager(["A"], journal=journal, client=client)
        await mgr.periodic_reconcile()

        assert _strat(mgr, "A").position.is_flat()

    @pytest.mark.asyncio
    async def test_phantom_corrected_to_flat(self, journal):
        """Local has position, broker doesn't -> reset to flat, persist."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])

        mgr = _build_manager(["A"], journal=journal, client=client)
        _strat(mgr, "A").position.open(Direction.LONG, 1.0, 100.0)

        await mgr.periodic_reconcile()

        assert _strat(mgr, "A").position.is_flat()
        loaded = journal.load()
        assert loaded is not None
        assert loaded[0].direction is None

    @pytest.mark.asyncio
    async def test_failed_exit_adopted_and_evaluated(self, journal):
        """Local flat, broker has position -> adopt, then evaluate exits."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "SELL", 2.0, 80.0)])
        client.get_historical_candles = AsyncMock(return_value=[_candle()])

        mgr = _build_manager(["A"], journal=journal, client=client)
        await mgr.periodic_reconcile()

        strat = _strat(mgr, "A")
        assert strat.position.direction == Direction.SHORT
        assert strat.position.size == 2.0
        assert strat._check_exits_called

    @pytest.mark.asyncio
    async def test_size_mismatch_corrected(self, journal):
        """Local and broker disagree on size -> adopt broker size."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "BUY", 3.0, 100.0)])

        mgr = _build_manager(["A"], journal=journal, client=client)
        _strat(mgr, "A").position.open(Direction.LONG, 1.0, 100.0)

        await mgr.periodic_reconcile()

        assert _strat(mgr, "A").position.size == 3.0
        loaded = journal.load()
        entry = next(e for e in loaded if e.instrument == "A")
        assert entry.size == 3.0

    @pytest.mark.asyncio
    async def test_direction_mismatch_corrected(self, journal):
        """Local long, broker SELL -> adopt broker direction, evaluate exits."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("A", "SELL", 1.0, 90.0)])
        client.get_historical_candles = AsyncMock(return_value=[_candle()])

        mgr = _build_manager(["A"], journal=journal, client=client)
        _strat(mgr, "A").position.open(Direction.LONG, 1.0, 100.0)

        await mgr.periodic_reconcile()

        strat = _strat(mgr, "A")
        assert strat.position.direction == Direction.SHORT
        assert strat._check_exits_called

    @pytest.mark.asyncio
    async def test_recently_changed_epic_skipped(self, journal):
        """Epics with recent position changes are excluded from reconcile."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])  # broker says flat

        mgr = _build_manager(["A"], journal=journal, client=client)
        _strat(mgr, "A").position.open(Direction.LONG, 1.0, 100.0)
        mgr._recently_changed_instruments.add("A")

        await mgr.periodic_reconcile()

        # Should NOT have been corrected
        assert not _strat(mgr, "A").position.is_flat()

    @pytest.mark.asyncio
    async def test_recently_changed_cleared_after_reconcile(self, journal):
        """Recently changed set is cleared after periodic reconciliation runs."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[])

        mgr = _build_manager(["A"], journal=journal, client=client)
        mgr._recently_changed_instruments.add("A")

        await mgr.periodic_reconcile()

        assert len(mgr._recently_changed_instruments) == 0

    @pytest.mark.asyncio
    async def test_broker_api_failure_skips_reconcile(self, journal):
        """When broker API fails, periodic reconciliation is skipped."""
        client = AsyncMock()
        client.get_positions = AsyncMock(side_effect=RuntimeError("HTTP 500"))

        mgr = _build_manager(["A"], journal=journal, client=client)
        _strat(mgr, "A").position.open(Direction.LONG, 1.0, 100.0)

        # Should not crash or modify state
        await mgr.periodic_reconcile()
        assert _strat(mgr, "A").position.direction == Direction.LONG

    @pytest.mark.asyncio
    async def test_multiple_epics_independent(self, journal):
        """Corrections apply per-epic: one phantom, one matched."""
        client = AsyncMock()
        client.get_positions = AsyncMock(return_value=[_bp("B", "SELL", 0.5, 50.0)])

        mgr = _build_manager(["A", "B"], journal=journal, client=client)
        _strat(mgr, "A").position.open(Direction.LONG, 1.0, 100.0)  # phantom
        _strat(mgr, "B").position.open(Direction.SHORT, 0.5, 50.0)  # matched

        await mgr.periodic_reconcile()

        assert _strat(mgr, "A").position.is_flat()  # corrected
        assert _strat(mgr, "B").position.direction == Direction.SHORT  # unchanged


# ---------------------------------------------------------------------------
# Position change callback wiring
# ---------------------------------------------------------------------------


class TestPositionChangeCallback:
    def test_persist_tracks_changed_epic(self, journal):
        """persist_positions records which epic triggered the save."""
        mgr = _build_manager(["A", "B"], journal=journal)
        mgr.persist_positions("A")

        assert "A" in mgr._recently_changed_instruments

    def test_notify_fires_callback_with_epic(self, journal):
        """Strategy.notify_position_change passes epic to callback."""
        mgr = _build_manager(["A"], journal=journal)
        strat = _strat(mgr, "A")
        strat.position.open(Direction.LONG, 1.0, 100.0)
        strat.notify_position_change()

        assert "A" in mgr._recently_changed_instruments
        loaded = journal.load()
        assert loaded is not None
        assert loaded[0].direction == "long"
