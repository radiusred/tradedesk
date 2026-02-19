"""Tests for tradedesk.portfolio.journal – position journal for crash recovery."""

import json

import pytest

from tradedesk.portfolio.journal import JournalEntry, PositionJournal


@pytest.fixture
def journal_dir(tmp_path):
    return tmp_path / "journal"


@pytest.fixture
def journal(journal_dir):
    return PositionJournal(journal_dir)


def _make_entry(**overrides):
    defaults = dict(
        instrument="USDJPY",
        direction="long",
        size=1.0,
        entry_price=150.0,
        bars_held=5,
        mfe_points=2.0,
        entry_atr=0.8,
        updated_at="2025-01-15T12:00:00Z",
    )
    defaults.update(overrides)
    return JournalEntry(**defaults)


class TestPositionJournal:
    def test_save_creates_directory_and_file(self, journal, journal_dir):
        entries = [_make_entry()]
        journal.save(entries)
        assert (journal_dir / "positions.json").exists()

    def test_save_and_load_round_trip(self, journal):
        original = [_make_entry(), _make_entry(instrument="GBPUSD", direction="short")]
        journal.save(original)

        loaded = journal.load()
        assert loaded is not None
        assert len(loaded) == 2
        assert loaded[0].instrument == "USDJPY"
        assert loaded[0].direction == "long"
        assert loaded[1].instrument == "GBPUSD"
        assert loaded[1].direction == "short"

    def test_load_returns_none_when_no_file(self, journal):
        assert journal.load() is None

    def test_load_backward_compat_epic_to_instrument(self, journal, journal_dir):
        """Legacy journals used 'epic' instead of 'instrument'."""
        journal_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "created_at": "2025-01-01T00:00:00Z",
            "positions": [
                {
                    "epic": "USDJPY",
                    "direction": "long",
                    "size": 1.0,
                    "entry_price": 150.0,
                    "bars_held": 3,
                    "mfe_points": 1.0,
                    "entry_atr": 0.5,
                    "updated_at": "2025-01-01T00:00:00Z",
                }
            ],
        }
        (journal_dir / "positions.json").write_text(json.dumps(data))

        loaded = journal.load()
        assert loaded is not None
        assert loaded[0].instrument == "USDJPY"

    def test_load_returns_none_on_corrupt_json(self, journal, journal_dir):
        journal_dir.mkdir(parents=True, exist_ok=True)
        (journal_dir / "positions.json").write_text("not valid json!!!")
        assert journal.load() is None

    def test_clear_removes_file(self, journal, journal_dir):
        journal.save([_make_entry()])
        assert (journal_dir / "positions.json").exists()
        journal.clear()
        assert not (journal_dir / "positions.json").exists()

    def test_clear_noop_when_no_file(self, journal):
        # Should not raise
        journal.clear()

    def test_save_atomic_write(self, journal, journal_dir):
        """Verify that the temp file is cleaned up after rename."""
        journal.save([_make_entry()])
        assert not (journal_dir / ".positions.json.tmp").exists()

    def test_flat_entry_round_trip(self, journal):
        """An entry with direction=None (flat) should round-trip correctly."""
        entries = [_make_entry(direction=None, size=None, entry_price=None)]
        journal.save(entries)
        loaded = journal.load()
        assert loaded[0].direction is None
        assert loaded[0].size is None
