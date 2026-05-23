"""Observability metrics for portfolio reconciliation.

Counters are exposed as Prometheus metrics when ``prometheus_client`` is
installed. When the library is missing the helpers degrade to no-ops so
tradedesk itself does not gain a hard dependency on it.
"""

from __future__ import annotations

from typing import Any


class _NoopMetric:
    def labels(self, *_: Any, **__: Any) -> "_NoopMetric":
        return self

    def inc(self, _amount: float = 1.0) -> None:
        return None


try:
    from prometheus_client import Counter

    RECONCILIATION_FAILURES: Any = Counter(
        "tradedesk_reconciliation_failures_total",
        "Reconciliation operations that failed due to broker/journal errors",
        ["operation"],
    )
except ImportError:  # pragma: no cover - exercised only when dep missing
    RECONCILIATION_FAILURES = _NoopMetric()


__all__ = ["RECONCILIATION_FAILURES"]
