"""Position reconciliation between local journal and broker state.

The package is split into:

* :mod:`.result` -- pure types and the comparison algorithm.
* :mod:`.policy` -- how decisions translate into strategy mutations.
* :mod:`.manager` -- :class:`ReconciliationManager`, the event-subscription glue.
"""

from .manager import ReconciliationManager
from .result import (
    DiscrepancyType,
    ReconciliationEntry,
    ReconciliationResult,
    _direction_matches,
    reconcile,
)

__all__ = [
    "DiscrepancyType",
    "ReconciliationEntry",
    "ReconciliationManager",
    "ReconciliationResult",
    "_direction_matches",
    "reconcile",
]
