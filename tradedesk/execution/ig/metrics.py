"""Resilience metrics for the IG execution layer.

Counters and gauges are exposed as Prometheus metrics when ``prometheus_client``
is installed (the ig_trader runtime declares it as a dependency and starts the
``/metrics`` exporter). When the library is not available, the helpers degrade
to no-ops so tradedesk itself does not gain a hard dependency on it.
"""

from __future__ import annotations

from typing import Any, Final, Protocol

LATENCY_BUCKETS_S: Final[tuple[float, ...]] = (
    30.0,
    60.0,
    120.0,
    300.0,
    600.0,
    1800.0,
    3600.0,
)


class _Counter(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...


class _Gauge(Protocol):
    def set(self, value: float) -> None: ...
    def inc(self, amount: float = 1.0) -> None: ...
    def dec(self, amount: float = 1.0) -> None: ...


class _LabelledCounter(Protocol):
    def labels(self, *args: Any, **kwargs: Any) -> _Counter: ...


class _LabelledGauge(Protocol):
    def labels(self, *args: Any, **kwargs: Any) -> _Gauge: ...


class _NoopMetric:
    """Stand-in used when prometheus_client is not installed."""

    def labels(self, *_: Any, **__: Any) -> "_NoopMetric":
        return self

    def inc(self, _amount: float = 1.0) -> None:  # noqa: D401
        return None

    def set(self, _value: float) -> None:
        return None

    def dec(self, _amount: float = 1.0) -> None:
        return None

    def observe(self, _value: float) -> None:
        return None


try:
    from prometheus_client import Counter, Gauge, Histogram

    STREAM_RECONNECTS: Any = Counter(
        "tradedesk_ig_stream_reconnects_total",
        "IG Lightstreamer reconnect attempts",
        ["reason"],
    )
    STREAM_RECONNECT_RECOVERIES: Any = Counter(
        "tradedesk_ig_stream_reconnect_recoveries_total",
        "IG Lightstreamer reconnects that recovered a productive stream "
        "(reconnect session received at least one update before going stale)",
    )
    STREAM_STALE_SECONDS: Any = Histogram(
        "tradedesk_ig_stream_stale_seconds",
        "Observed stale-stream durations before reconnect (seconds)",
        buckets=LATENCY_BUCKETS_S,
    )
    SUBSCRIPTION_RETRIES: Any = Counter(
        "tradedesk_ig_subscription_retries_total",
        "IG Lightstreamer subscription retry attempts",
        ["kind"],
    )
    AUTH_REFRESHES: Any = Counter(
        "tradedesk_ig_auth_refreshes_total",
        "IG OAuth/session refreshes",
        ["outcome"],
    )
    AUTH_REFRESH_INFLIGHT: Any = Gauge(
        "tradedesk_ig_auth_refresh_inflight",
        "Number of OAuth refresh requests currently in flight",
    )
    REQUEST_ERRORS: Any = Counter(
        "tradedesk_ig_request_errors_total",
        "IG REST request failures by HTTP status and IG errorCode",
        ["status", "error_code"],
    )
    REQUEST_AUTH_RETRIES: Any = Counter(
        "tradedesk_ig_request_auth_retries_total",
        "IG REST requests re-issued after a 401/403 re-authentication",
        ["outcome"],
    )
except ImportError:  # pragma: no cover - exercised only when dep missing
    _noop = _NoopMetric()
    STREAM_RECONNECTS = _noop
    STREAM_RECONNECT_RECOVERIES = _noop
    STREAM_STALE_SECONDS = _noop
    SUBSCRIPTION_RETRIES = _noop
    AUTH_REFRESHES = _noop
    AUTH_REFRESH_INFLIGHT = _noop
    REQUEST_ERRORS = _noop
    REQUEST_AUTH_RETRIES = _noop


__all__ = [
    "AUTH_REFRESHES",
    "AUTH_REFRESH_INFLIGHT",
    "REQUEST_AUTH_RETRIES",
    "REQUEST_ERRORS",
    "STREAM_RECONNECTS",
    "STREAM_RECONNECT_RECOVERIES",
    "STREAM_STALE_SECONDS",
    "SUBSCRIPTION_RETRIES",
]
