"""Operational tuning constants for tradedesk runtime behaviour.

Centralises the timeouts, retry counts, and polling cadences that govern
streaming, order confirmation, and authentication.  Each constant has an
environment-variable override so an operator can tune behaviour without
editing code.

Conventions
-----------
* Suffix ``_S`` denotes a duration in **seconds** (float).
* Suffix ``_RETRIES`` / ``_ATTEMPTS`` denotes a **count** (int).
* Helpers ``_env_float`` / ``_env_int`` accept the override env var and
  fall back to the default if unset or unparseable.

Note
----
For IG **credentials** (API key, username, password, environment) see
``tradedesk/execution/ig/settings.py``.  This module covers tunables that
shape connection behaviour, not user identity.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    """Return env var ``name`` as float, or ``default`` if unset/unparseable."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("Invalid float for %s=%r; using default %r", name, raw, default)
        return default


def _env_int(name: str, default: int) -> int:
    """Return env var ``name`` as int, or ``default`` if unset/unparseable."""
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid int for %s=%r; using default %r", name, raw, default)
        return default


# ---------------------------------------------------------------------------
# Lightstreamer (price stream) tunables
# ---------------------------------------------------------------------------

# Maximum retry attempts for a failed Lightstreamer subscription before
# giving up and logging an error.  Count.
STREAM_SUB_MAX_RETRIES: int = _env_int("TRADEDESK_STREAM_SUB_MAX_RETRIES", 3)

# Base delay between subscription retries; actual delay is
# ``attempt * STREAM_SUB_RETRY_BASE_DELAY_S``.  Seconds.
STREAM_SUB_RETRY_BASE_DELAY_S: float = _env_float(
    "TRADEDESK_STREAM_SUB_RETRY_BASE_DELAY_S", 2.0
)

# Heartbeat monitor sleep cadence — how often the staleness check runs.
# Seconds.
STREAM_HEARTBEAT_SLEEP_S: int = _env_int("TRADEDESK_STREAM_HEARTBEAT_SLEEP_S", 10)

# Default ceiling on stream silence before reconnect is initiated.
# Overridden per-instance via ``Lightstreamer(max_stale_seconds=...)``.
# Seconds.
STREAM_MAX_STALE_DEFAULT_S: float = _env_float(
    "TRADEDESK_STREAM_MAX_STALE_S", 300.0
)

# Default delay between reconnect attempts after a stale-stream event.
# Overridden per-instance via ``Lightstreamer(reconnect_delay=...)``.
# Seconds.
STREAM_RECONNECT_DELAY_DEFAULT_S: float = _env_float(
    "TRADEDESK_STREAM_RECONNECT_DELAY_S", 5.0
)

# Stream silence threshold beyond which heartbeat warnings are suppressed
# until data resumes (avoids log spam during weekend market closes).
# Seconds.
STREAM_SILENCE_SUPPRESS_THRESHOLD_S: float = _env_float(
    "TRADEDESK_STREAM_SILENCE_SUPPRESS_S", 300.0
)

# Sleep cadence used when heartbeat warnings are suppressed.  Seconds.
STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S: int = _env_int(
    "TRADEDESK_STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S", 60
)

# ---------------------------------------------------------------------------
# IG REST: authentication & order confirmation
# ---------------------------------------------------------------------------

# Minimum interval between successive IG /session auth attempts.  Used to
# protect against the IG public-API key allowance.  Seconds.
IG_AUTH_MIN_INTERVAL_S: float = _env_float("IG_AUTH_MIN_INTERVAL_S", 5.0)

# Maximum time to poll /confirms/{ref} for a non-PENDING dealStatus before
# raising TimeoutError.  Seconds.
IG_DEAL_CONFIRM_TIMEOUT_S: float = _env_float("IG_DEAL_CONFIRM_TIMEOUT_S", 10.0)

# Sleep between successive /confirms polls.  Seconds.
IG_DEAL_CONFIRM_POLL_S: float = _env_float("IG_DEAL_CONFIRM_POLL_S", 0.25)

# ---------------------------------------------------------------------------
# Order request bus
# ---------------------------------------------------------------------------

# Safety-net timeout for ``request_order`` to wait for the order handler
# to resolve the future.  In normal operation the handler resolves
# synchronously inside ``publish()``.  Seconds.
ORDER_REQUEST_TIMEOUT_S: float = _env_float("TRADEDESK_ORDER_REQUEST_TIMEOUT_S", 30.0)
