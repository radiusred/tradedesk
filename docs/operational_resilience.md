# Operational Resilience and Monitoring

tradedesk is designed for production use with built-in resilience mechanisms and operational monitoring.

## Resilience Features

### Asyncio-Based Retry Scheduler

Subscription retries (price stream reconnects) use an asyncio-based `RetryScheduler` that is cancellable when the streamer closes. This replaces earlier threading-based approaches and ensures clean, deterministic shutdown without orphaned threads.

**Behavior**:
- Linear backoff: `delay = attempt * STREAM_SUB_RETRY_BASE_DELAY_S`
- Maximum retries: `STREAM_SUB_MAX_RETRIES` (default: 3)
- Automatic cancellation on streamer shutdown

**Configuration**: See `docs/settings.md` for `STREAM_SUB_*` environment variables.

### Single-Flight OAuth Refresh

When multiple concurrent callers need to refresh an expired session token, they share a single `/session` authentication request instead of racing and creating duplicate tokens. This avoids:
- IG API rate limits on session creation
- Transient auth failures during high concurrency
- Wasted network requests

**Automatic**: No configuration needed. Token state is managed internally by `TokenState` lifecycle.

### SIGTERM/SIGHUP Signal Handling

Live runners install signal handlers that cleanly shut down the portfolio when receiving `SIGTERM` (container stop, systemd shutdown) or `SIGHUP` (terminal disconnect):

1. Signal handler sets shutdown event
2. Event loop processes pending orders and closes connections gracefully
3. Open positions remain open (not closed automatically)
4. Process exits cleanly without orphaned tasks

**Example**: Kubernetes container stop → SIGTERM → portfolio shutdown → graceful exit.

**Platform note**: Signal handlers are installed on POSIX systems (Linux, macOS). Windows runners fall back to `KeyboardInterrupt` only.

### Stale Stream Detection and Reconnect

The Lightstreamer price stream includes a heartbeat monitor that detects stale connections:

- **Detection**: Monitor checks stream age every `STREAM_HEARTBEAT_SLEEP_S` seconds
- **Threshold**: If no data for `STREAM_MAX_STALE_S` seconds (default: 300s), reconnect is initiated
- **Reconnect delay**: `STREAM_RECONNECT_DELAY_S` between attempts (default: 5s)
- **Log suppression**: During expected market closures (>300s silence), warnings are suppressed to avoid spam

**Tuning**: Adjust `STREAM_MAX_STALE_S` for high-latency or unreliable networks.

## Operational Metrics

tradedesk emits Prometheus metrics for operational visibility. Metrics are **lazily imported** — no hard dependency on `prometheus_client`.

### Available Metrics

#### Counter: `tradedesk_ig_auth_refreshes_total`
Incremented each time a session token is refreshed. Labels: `outcome` (success/failure).

#### Gauge: `tradedesk_ig_auth_refresh_inflight`
Number of in-flight authentication refresh requests. Tracks single-flight OAuth mechanism effectiveness. No labels.

#### Counter: `tradedesk_ig_subscription_retries_total`
Incremented for each subscription retry attempt on the price stream. Tracks unreliable subscription requests. Labels: `kind` (retry type).

#### Counter: `tradedesk_ig_stream_reconnects_total`
Incremented each time the price stream reconnects. High counts may indicate network issues. Labels: `reason` (why reconnect was triggered).

#### Histogram: `tradedesk_ig_stream_stale_seconds`
Duration of stream silence before reconnect. Tracks impact of connectivity issues on data delivery.

### Enabling Prometheus Metrics

Metrics are available if `prometheus_client` is installed:

```bash
pip install prometheus_client
```

Metrics are collected lazily and require no explicit configuration. Access them via the standard Prometheus client:

```python
from prometheus_client import REGISTRY

# Export metrics
print(REGISTRY.collect())
```

### Example: Scraping in Prometheus

Configure a Prometheus scrape endpoint in your runner:

```python
from prometheus_client import start_http_server

# Start Prometheus metrics HTTP server on port 8000
start_http_server(8000)

# Now run your portfolio
portfolio = MyPortfolio(...)
await portfolio.run(...)
```

Then configure Prometheus to scrape `http://localhost:8000/metrics`.

## Operational Deployment Patterns

### Container Orchestration (Kubernetes)

Deploy tradedesk runners in containers with these best practices:

1. **Mount IG credentials via secrets**: Use environment variables from ConfigMaps/Secrets, not hardcoded
2. **Graceful shutdown**: Set `terminationGracePeriodSeconds` to 60+ (allow time for orders to clear)
3. **Resource requests**: Set appropriate CPU/memory limits
4. **Signal handling**: SIGTERM is automatically handled; no custom shutdown scripts needed

Example Kubernetes manifest excerpt:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: tradedesk-runner
spec:
  terminationGracePeriodSeconds: 60
  containers:
  - name: runner
    image: my-tradedesk-runner:latest
    env:
    - name: IG_API_KEY
      valueFrom:
        secretKeyRef:
          name: ig-credentials
          key: api-key
    - name: IG_USERNAME
      valueFrom:
        secretKeyRef:
          name: ig-credentials
          key: username
    - name: IG_PASSWORD
      valueFrom:
        secretKeyRef:
          name: ig-credentials
          key: password
    - name: IG_ENVIRONMENT
      value: "DEMO"
    - name: TRADEDESK_STREAM_MAX_STALE_S
      value: "300"
    resources:
      requests:
        cpu: "500m"
        memory: "512Mi"
      limits:
        cpu: "1000m"
        memory: "1Gi"
```

### Systemd Service

For systemd-managed services, SIGTERM is sent on shutdown:

```ini
[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/tradedesk/runner.py
Environment="IG_ENVIRONMENT=LIVE"
# Allow 60 seconds for graceful shutdown
TimeoutStopSec=60
# Restart on failure, but not immediately
Restart=on-failure
RestartSec=30
```

## Failure Modes and Recovery

### Stream Disconnection

**Symptom**: Prices stop updating, log shows "stream stale".

**Response**:
1. Monitor detects silence after `STREAM_MAX_STALE_S`
2. Streamer initiates reconnect
3. Subscriptions are retried up to `STREAM_SUB_MAX_RETRIES` times
4. If max retries exceeded, error is logged and positions remain open

**Action**: Check network connectivity, IG service status, and review logs.

### Authentication Failure

**Symptom**: Log shows "auth failed" or "invalid session".

**Response**:
1. `TokenState` triggers refresh on next call
2. Single-flight mechanism ensures one `/session` request per refresh cycle
3. If refresh fails, subsequent orders may be rejected

**Action**: Verify IG credentials are valid and account permissions allow API access.

### Order Confirmation Timeout

**Symptom**: Order placed but deal confirmation never arrives.

**Response**:
1. `IG_DEAL_CONFIRM_TIMEOUT_S` expires (default 10 seconds)
2. `TimeoutError` is raised to caller
3. Position state is inconsistent until manual intervention

**Action**: Increase `IG_DEAL_CONFIRM_TIMEOUT_S` on slow connections, or investigate IG service issues.

## See Also

- `docs/settings.md` — Tunable parameters and environment variables
- `tradedesk/execution/ig/metrics.py` — Prometheus metric definitions
- `tradedesk/runner.py` — Signal handler and portfolio lifecycle
- `tradedesk/execution/ig/price_streamer.py` — Stream reconnection logic
