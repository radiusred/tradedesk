# Operational Tunables and Settings

tradedesk centralizes operational tunables — timeouts, retry counts, and polling cadences — in the `tradedesk.settings` module. Each setting has an environment variable override so operators can adjust behaviour without editing code.

## Naming Conventions

* Suffix `_S` denotes a duration in **seconds** (float).
* Suffix `_RETRIES` / `_ATTEMPTS` denotes a **count** (int).
* Environment variable overrides are case-sensitive and default to sensible values if unset or unparseable.

For **IG credentials** (API key, username, password, environment, account ID), see `tradedesk.execution.ig.settings`.

## Lightstreamer (Price Stream) Tunables

### `TRADEDESK_STREAM_SUB_MAX_RETRIES` (default: 3)
Maximum retry attempts for a failed Lightstreamer subscription before giving up and logging an error. Count.

### `TRADEDESK_STREAM_SUB_RETRY_BASE_DELAY_S` (default: 2.0)
Base delay between subscription retries; actual delay is `attempt * STREAM_SUB_RETRY_BASE_DELAY_S`. Seconds.

Example: with 3 retry attempts and base delay of 2 seconds, actual delays are 2s, 4s, 6s.

### `TRADEDESK_STREAM_HEARTBEAT_SLEEP_S` (default: 10)
Heartbeat monitor sleep cadence — how often the staleness check runs. Seconds.

### `TRADEDESK_STREAM_MAX_STALE_S` (default: 300.0)
Default ceiling on stream silence before reconnect is initiated. Can be overridden per-instance via `Lightstreamer(max_stale_seconds=...)`. Seconds.

### `TRADEDESK_STREAM_RECONNECT_DELAY_S` (default: 5.0)
Default delay between reconnect attempts after a stale-stream event. Can be overridden per-instance via `Lightstreamer(reconnect_delay=...)`. Seconds.

### `TRADEDESK_STREAM_SILENCE_SUPPRESS_S` (default: 300.0)
Stream silence threshold beyond which heartbeat warnings are suppressed until data resumes. Avoids log spam during weekend market closes. Seconds.

### `TRADEDESK_STREAM_HEARTBEAT_SUPPRESSED_SLEEP_S` (default: 60)
Sleep cadence used when heartbeat warnings are suppressed. Seconds.

## IG REST: Authentication & Order Confirmation

### `IG_AUTH_MIN_INTERVAL_S` (default: 5.0)
Minimum interval between successive IG `/session` auth attempts. Used to protect against the IG public-API key allowance. Seconds.

**Important**: Do not reduce this below 5 seconds; IG enforces rate limits on session creation.

### `IG_DEAL_CONFIRM_TIMEOUT_S` (default: 10.0)
Maximum time to poll `/confirms/{ref}` for a non-PENDING dealStatus before raising `TimeoutError`. Seconds.

### `IG_DEAL_CONFIRM_POLL_S` (default: 0.25)
Sleep between successive `/confirms` polls. Seconds.

## Order Request Bus

### `TRADEDESK_ORDER_REQUEST_TIMEOUT_S` (default: 30.0)
Safety-net timeout for `request_order()` to wait for the order handler to resolve the future. In normal operation the handler resolves synchronously inside `publish()`. Seconds.

## Usage Example

Override settings via environment variables:

```bash
TRADEDESK_STREAM_MAX_STALE_S=60.0 \
IG_AUTH_MIN_INTERVAL_S=10.0 \
TRADEDESK_ORDER_REQUEST_TIMEOUT_S=60.0 \
python your_live_runner.py
```

Unparseable values are logged with a warning and the default is used:

```python
TRADEDESK_STREAM_MAX_STALE_S=not_a_number python your_runner.py
# WARNING:tradedesk.settings:Invalid float for TRADEDESK_STREAM_MAX_STALE_S='not_a_number'; using default 300.0
```

## Tuning Guidelines

* **For reliable price streams**: Increase `TRADEDESK_STREAM_MAX_STALE_S` if you experience frequent reconnects on high-latency connections (300s → 600s).
* **For order confirmation**: Increase `IG_DEAL_CONFIRM_TIMEOUT_S` if IG experiences elevated confirmation latency (10s → 20s).
* **For backtest performance**: Settings have no effect on backtest runs; they only govern live broker behaviour.

## See Also

* `tradedesk/settings.py` — source code with inline defaults
* `tradedesk/execution/ig/settings.py` — IG credential and broker-specific settings
