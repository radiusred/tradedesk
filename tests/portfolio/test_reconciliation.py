"""Tests for tradedesk.portfolio.reconciliation."""

from unittest.mock import AsyncMock, Mock

import pytest

from tradedesk import Direction
from tradedesk.execution.broker import BrokerPosition
from tradedesk.portfolio.journal import JournalEntry
from tradedesk.portfolio.reconciliation import (
    DiscrepancyType,
    ReconciliationManager,
    ReconciliationResult,
    _direction_matches,
    reconcile,
)
from tradedesk.portfolio.types import Instrument, SleeveId


def _journal_entry(instrument="USDJPY", direction="long", size=1.0):
    return JournalEntry(
        instrument=instrument,
        direction=direction,
        size=size,
        entry_price=150.0,
        bars_held=5,
        mfe_points=2.0,
        entry_atr=0.8,
        updated_at="2025-01-15T12:00:00Z",
    )


def _broker_pos(instrument="USDJPY", direction="BUY", size=1.0):
    return BrokerPosition(
        instrument=instrument,
        direction=direction,
        size=size,
        entry_price=150.0,
        deal_id="DEAL123",
    )


class TestDirectionMatches:
    def test_long_buy(self):
        assert _direction_matches("long", "BUY") is True

    def test_short_sell(self):
        assert _direction_matches("short", "SELL") is True

    def test_long_sell(self):
        assert _direction_matches("long", "SELL") is False

    def test_none_direction(self):
        assert _direction_matches(None, "BUY") is False


class TestReconcile:
    def test_both_flat(self):
        result = reconcile(
            journal_positions={"USDJPY": _journal_entry(direction=None)},
            broker_positions=[],
            managed_instruments={"USDJPY"},
        )
        assert result.is_clean
        assert len(result.entries) == 1
        assert result.entries[0].discrepancy == DiscrepancyType.MATCHED

    def test_matched_positions(self):
        result = reconcile(
            journal_positions={"USDJPY": _journal_entry(direction="long", size=1.0)},
            broker_positions=[_broker_pos(direction="BUY", size=1.0)],
            managed_instruments={"USDJPY"},
        )
        assert result.is_clean

    def test_direction_mismatch(self):
        result = reconcile(
            journal_positions={"USDJPY": _journal_entry(direction="long")},
            broker_positions=[_broker_pos(direction="SELL")],
            managed_instruments={"USDJPY"},
        )
        assert not result.is_clean
        assert result.entries[0].discrepancy == DiscrepancyType.DIRECTION_MISMATCH

    def test_size_mismatch(self):
        result = reconcile(
            journal_positions={"USDJPY": _journal_entry(direction="long", size=1.0)},
            broker_positions=[_broker_pos(direction="BUY", size=2.0)],
            managed_instruments={"USDJPY"},
        )
        assert not result.is_clean
        assert result.entries[0].discrepancy == DiscrepancyType.SIZE_MISMATCH

    def test_orphan_broker_position(self):
        result = reconcile(
            journal_positions={},
            broker_positions=[_broker_pos(instrument="USDJPY")],
            managed_instruments={"USDJPY"},
        )
        assert not result.is_clean
        orphans = result.orphan_broker_positions
        assert len(orphans) == 1
        assert orphans[0].instrument == "USDJPY"

    def test_phantom_local_position(self):
        result = reconcile(
            journal_positions={"USDJPY": _journal_entry(direction="long")},
            broker_positions=[],
            managed_instruments={"USDJPY"},
        )
        assert not result.is_clean
        phantoms = result.phantom_local_positions
        assert len(phantoms) == 1
        assert phantoms[0].instrument == "USDJPY"

    def test_failed_exit(self):
        """Journal says flat (direction=None) but broker still has the position."""
        result = reconcile(
            journal_positions={"USDJPY": _journal_entry(direction=None)},
            broker_positions=[_broker_pos(instrument="USDJPY")],
            managed_instruments={"USDJPY"},
        )
        assert result.has_emergencies
        assert result.entries[0].discrepancy == DiscrepancyType.FAILED_EXIT

    def test_unmanaged_instrument_ignored(self):
        """Broker positions for unmanaged instruments should be ignored."""
        result = reconcile(
            journal_positions={},
            broker_positions=[_broker_pos(instrument="UNMANAGED")],
            managed_instruments={"USDJPY"},
        )
        assert result.is_clean

    def test_multiple_instruments(self):
        result = reconcile(
            journal_positions={
                "USDJPY": _journal_entry(instrument="USDJPY", direction="long", size=1.0),
                "GBPUSD": _journal_entry(instrument="GBPUSD", direction="short", size=2.0),
            },
            broker_positions=[
                _broker_pos(instrument="USDJPY", direction="BUY", size=1.0),
                _broker_pos(instrument="GBPUSD", direction="SELL", size=2.0),
            ],
            managed_instruments={"USDJPY", "GBPUSD"},
        )
        assert result.is_clean


class TestReconciliationResult:
    def test_is_clean(self):
        from tradedesk.portfolio.reconciliation import ReconciliationEntry

        result = ReconciliationResult(
            entries=[
                ReconciliationEntry(instrument="X", discrepancy=DiscrepancyType.MATCHED),
            ]
        )
        assert result.is_clean

    def test_has_emergencies(self):
        from tradedesk.portfolio.reconciliation import ReconciliationEntry

        result = ReconciliationResult(
            entries=[
                ReconciliationEntry(instrument="X", discrepancy=DiscrepancyType.FAILED_EXIT),
            ]
        )
        assert result.has_emergencies

    def test_orphan_broker_positions(self):
        from tradedesk.portfolio.reconciliation import ReconciliationEntry

        result = ReconciliationResult(
            entries=[
                ReconciliationEntry(instrument="X", discrepancy=DiscrepancyType.ORPHAN_BROKER),
                ReconciliationEntry(instrument="Y", discrepancy=DiscrepancyType.MATCHED),
            ]
        )
        assert len(result.orphan_broker_positions) == 1

    def test_phantom_local_positions(self):
        from tradedesk.portfolio.reconciliation import ReconciliationEntry

        result = ReconciliationResult(
            entries=[
                ReconciliationEntry(instrument="X", discrepancy=DiscrepancyType.PHANTOM_LOCAL),
            ]
        )
        assert len(result.phantom_local_positions) == 1


class TestReconciliationManager:
    @pytest.fixture
    def mock_strategy(self):
        strat = Mock()
        strat.instrument = Instrument("USDJPY")
        strat.position = Mock()
        strat.position.is_flat.return_value = True
        strat.position.direction = None
        strat.position.size = 0.0
        # Default behavior for to_journal_entry
        strat.to_journal_entry.side_effect = lambda inst: _journal_entry(
            instrument=inst, direction=None
        )
        strat.restore_from_journal = Mock()
        strat.check_restored_position = AsyncMock()
        return strat

    @pytest.fixture
    def mock_runner(self, mock_strategy):
        runner = Mock()
        # Populate with one default strategy, keyed by SleeveId
        runner.strategies = {SleeveId("USDJPY"): mock_strategy}
        return runner

    @pytest.fixture
    def mock_client(self):
        client = Mock()
        client.get_positions = AsyncMock(return_value=[])
        client.get_historical_candles = AsyncMock(return_value=[])
        client.get_account_balance = AsyncMock()
        return client

    @pytest.fixture
    def mock_journal(self):
        journal = Mock()
        journal.load.return_value = []
        journal.save = Mock()
        return journal

    @pytest.fixture
    def manager(self, mock_runner, mock_client, mock_journal):
        return ReconciliationManager(
            runner=mock_runner,
            client=mock_client,
            journal=mock_journal,
            target_period="15MINUTE",
            reconcile_interval=2,
            enable_event_subscription=False,  # Disable for unit tests
        )

    @pytest.mark.asyncio
    async def test_startup_clean(self, manager, mock_client, mock_journal):
        """Test startup with no positions anywhere."""
        restored = await manager.reconcile_on_startup()

        assert len(restored) == 0
        mock_client.get_positions.assert_called_once()
        mock_journal.load.assert_called_once()
        # Should not save if clean
        mock_journal.save.assert_not_called()

    @pytest.mark.asyncio
    async def test_startup_orphan_adoption(self, manager, mock_client, mock_runner):
        """Broker has position, journal empty -> adopt broker position."""
        mock_client.get_positions.return_value = [
            _broker_pos(instrument="USDJPY", direction="BUY", size=1.0)
        ]

        restored = await manager.reconcile_on_startup()

        assert "USDJPY" in restored
        strat = mock_runner.strategies[SleeveId("USDJPY")]
        # Verify open called
        strat.position.open.assert_called_with(Direction.LONG, 1.0, 150.0)
        # Should save to persist the adoption
        manager._journal.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_startup_broker_failure(self, manager, mock_client, mock_journal, mock_runner):
        """Broker fails, restore from journal."""
        mock_client.get_positions.side_effect = Exception("Broker down")
        mock_journal.load.return_value = [_journal_entry(instrument="USDJPY", direction="long")]

        restored = await manager.reconcile_on_startup()

        assert "USDJPY" in restored
        strat = mock_runner.strategies[SleeveId("USDJPY")]
        strat.restore_from_journal.assert_called_once()

    @pytest.mark.asyncio
    async def test_periodic_reconcile_correction(self, manager, mock_client, mock_runner):
        """Periodic check finds discrepancy and fixes it."""
        # Setup: Strategy thinks flat, Broker has position
        mock_client.get_positions.return_value = [
            _broker_pos(instrument="USDJPY", direction="BUY", size=1.0)
        ]

        # Force reconcile
        await manager.periodic_reconcile()

        strat = mock_runner.strategies[SleeveId("USDJPY")]
        strat.position.open.assert_called_with(Direction.LONG, 1.0, 150.0)
        manager._journal.save.assert_called_once()

    @pytest.mark.asyncio
    async def test_post_warmup_check(self, manager, mock_client, mock_runner):
        """Verifies exit check on restored positions."""
        # Setup strategy to look like it has a position
        strat = mock_runner.strategies[SleeveId("USDJPY")]
        strat.position.is_flat.return_value = False

        mock_client.get_historical_candles.return_value = [Mock(close=155.0)]

        await manager.post_warmup_check({"USDJPY"})

        mock_client.get_historical_candles.assert_called_with("USDJPY", "15MINUTE", 1)
        strat.check_restored_position.assert_called_once()

    def test_should_reconcile(self, manager):
        # Interval is 2
        assert not manager.should_reconcile()  # 1
        assert manager.should_reconcile()  # 2
