"""Position journal for crash recovery and reconciliation."""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JournalEntry:
    """Snapshot of a single strategy's position state."""

    instrument: str
    direction: str | None  # "long" or "short" or None if flat
    size: float | None
    entry_price: float | None
    bars_held: int
    mfe_points: float
    entry_atr: float  # strategy-specific, needed for exit logic
    updated_at: str  # ISO timestamp of last update


class PositionJournal:
    """
    Persists portfolio position state to a JSON file.

    Write pattern:
      On every position state change (open, close), the orchestrator calls
      :meth:`save` with the current state of all strategies.  The file is
      written atomically (write to ``.tmp``, then rename).

    Read pattern:
      On startup, the orchestrator calls :meth:`load` to get the last known
      state.  Returns ``None`` if no journal exists (fresh start).
    """

    FILENAME = "positions.json"

    def __init__(self, journal_dir: Path):
        self._dir = journal_dir
        self._path = journal_dir / self.FILENAME
        self._tmp_path = journal_dir / f".{self.FILENAME}.tmp"

    def save(self, entries: list[JournalEntry]) -> None:
        """Atomically write the current position snapshot."""
        self._dir.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "positions": [asdict(e) for e in entries],
        }
        self._tmp_path.write_text(json.dumps(data, indent=2))
        self._tmp_path.rename(self._path)
        log.debug("Position journal saved: %d positions", len(entries))

    def load(self) -> list[JournalEntry] | None:
        """Load the last saved snapshot, or ``None`` if no journal exists."""
        if not self._path.exists():
            return None

        try:
            data = json.loads(self._path.read_text())
            entries = []
            for e in data.get("positions", []):
                # Backward compat: rename legacy "epic" key to "instrument"
                if "epic" in e and "instrument" not in e:
                    e["instrument"] = e.pop("epic")
                entries.append(JournalEntry(**e))
            return entries
        except Exception:
            log.exception("Failed to load position journal from %s", self._path)
            return None

    def clear(self) -> None:
        """Remove the journal file (e.g. after clean shutdown with all flat)."""
        if self._path.exists():
            self._path.unlink()
            log.info("Position journal cleared")
