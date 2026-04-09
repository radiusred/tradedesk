# Contributing to tradedesk

tradedesk is a public, open-source trading framework published on PyPI and intended to be useful to anyone,
regardless of how they choose to deploy their applications. This guide defines what is and is not acceptable
in the codebase.

---

## Public-Library Contract

tradedesk is **infrastructure code**, not a deployment artifact. It must work correctly whether the user runs
it on a laptop, a bare-metal server, a Kubernetes cluster, Cloud Run, AWS Lambda, or anything else.

### What is acceptable

| Category | Examples | Rule |
|---|---|---|
| Broker integration code | IG REST client, IG streaming, Dukascopy adapter | Acceptable — the library explicitly supports IG and Dukascopy as named integrations |
| Standard-library resilience | TCP reconnection, exponential back-off, connection health checks | Acceptable — these work everywhere |
| Configuration-driven behaviour | Timeouts, retry counts, log levels passed as constructor arguments | Acceptable — caller controls the behaviour |
| OS-agnostic signal handling | `signal.SIGTERM` with a documented caveat | Acceptable if documented; avoid if possible |

### What is NOT acceptable

| Category | Examples | Why |
|---|---|---|
| Platform process-exit-as-restart | `sys.exit()` as a reconnection strategy relying on Cloud Run / Docker / systemd to restart the process | Destroys the process for users not running under a process supervisor |
| Cloud-provider strings in library source | `Cloud Run`, `GCP`, `AWS`, `Azure`, `Kubernetes`, `K8s` in `tradedesk/**/*.py` | Signals a deployment assumption, not a library concern |
| Container/orchestration assumptions | Assuming a health-check endpoint, assuming restart-on-exit, assuming shared volumes | Only valid in operator-owned deployment code |
| Hard-coded infrastructure references | Region strings, project IDs, service account names | Belong in deployment config, never in the library |

### The key question for reviewers

> "If a user runs this library on a bare Python process with no process supervisor, does this code still behave correctly?"

If the answer is **no**, the approach needs to change.

---

## PR Review Checklist

Every PR must be reviewed against the items in `.github/pull_request_template.md`. The most critical item
for this library is the **public-library suitability gate**:

- Does any changed code assume a specific deployment platform (Cloud Run, Docker, systemd, GCP, AWS, Azure)?
- Does any changed code call `sys.exit()` or `os._exit()` in a non-test context for operational reasons?
- Are there any log strings or comments referencing cloud providers or container runtimes?

If yes to any of the above, the PR **must not be merged** until the approach is changed.

---

## IG-Specific vs Deployment-Specific: Examples

### Acceptable IG-specific code

```python
# tradedesk/execution/ig/streamer.py
class IGStreamer:
    async def _reconnect(self) -> None:
        """Reconnect to the IG Lightstreamer endpoint with back-off."""
        await asyncio.sleep(self._reconnect_delay)
        await self._connect()
```

This is broker-specific but deployment-agnostic — it reconnects without requiring the process to exit.

### NOT acceptable: deployment-specific code

```python
# BAD — assumes Cloud Run will restart the process
if silence_duration > threshold:
    logger.warning("No data for %ds — exiting so Cloud Run can restart", silence_duration)
    sys.exit(1)
```

Replace with a reconnection strategy or raise an exception the caller can handle:

```python
# GOOD — caller decides how to handle the stale stream
if silence_duration > threshold:
    raise StaleStreamError(f"No data received for {silence_duration}s")
```

---

## Commit Messages

Follow [Conventional Commits](https://www.conventionalcommits.org/). Match the style visible in `git log`.

Examples:
- `feat(execution): add IG reconnection with exponential back-off`
- `fix(streamer): handle stale stream via exception rather than process exit`
- `docs: add contributing guide and PR checklist`

---

## Code Quality

- Python 3.11+ required
- Code must pass `uv run ruff check tradedesk` and `uv run mypy tradedesk`
- Tests must pass on Python 3.11–3.14 with `uv run pytest`
- Coverage must remain above 85%
- Follow domain import rules in `pyproject.toml` (top-level re-exports only across domains)

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://github.com/radiusred) | [Contact](mailto:opensource@radiusred.uk)
