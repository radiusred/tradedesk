# Crash Recovery and Position Reconciliation

This guide describes the public crash-recovery behavior shipped in
`tradedesk.portfolio.ReconciliationManager`.

The framework persists local position state to a journal, compares that journal
with live broker positions on startup, and corrects local state when the two do
not match. It does not define broker-specific operator steps; those belong in
the runtime that embeds `tradedesk`.

---

## Components

Crash recovery in `tradedesk` has three moving parts:

1. `PositionJournal` stores one `JournalEntry` per managed instrument.
2. `ReconciliationManager` compares journal state with broker positions.
3. Strategies implement `ReconcilableStrategy` so positions can be restored or
   adopted and then re-checked after warmup.

The broker remains the source of truth. The journal is a recovery hint that
preserves local metadata such as size, direction, bars held, MFE, and entry ATR.

---

## Startup lifecycle

When event subscription is enabled, `ReconciliationManager` wires itself into
the portfolio session lifecycle:

1. `SessionStartedEvent` triggers `reconcile_on_startup()`.
2. The journal is loaded from disk if present.
3. The broker client is queried for current positions.
4. Local and broker state are compared instrument by instrument.
5. Corrected state is written back to the journal when needed.
6. `SessionReadyEvent` triggers `post_warmup_check()` for any restored or
   adopted positions.

That last step matters because a recovered position may already satisfy exit
conditions once fresh indicators are primed.

If that post-warmup price lookup fails, the framework logs the exception and
keeps running. That includes IG-specific history-quota failures such as
historical-data allowance exhaustion: the recovered position stays open until a
later check can evaluate it with fresh data.

---

## Discrepancy types

`tradedesk.portfolio.reconciliation.DiscrepancyType` defines the mismatch
categories handled by the framework.

### `MATCHED`

The journal and broker agree on direction and size. The strategy position is
restored from the journal so local metadata is preserved.

### `PHANTOM_LOCAL`

The journal says a position is open but the broker reports no position. The
local position is reset to flat.

### `ORPHAN_BROKER`

The broker has a position that is missing from the journal. The framework
adopts the broker position into local state and schedules a post-warmup exit
check.

### `SIZE_MISMATCH`

Both sides agree on direction but not size. The journal metadata is restored
and the local position size is corrected to the broker size.

### `DIRECTION_MISMATCH`

The journal and broker disagree on direction. Broker state wins and the local
position is reopened from broker data.

### `FAILED_EXIT`

The journal records flat state (`direction=None`) but the broker still has an
open position. This is treated as an emergency discrepancy: the broker
position is adopted and a critical log entry is emitted.

---

## Broker-unavailable fallback

If `client.get_positions()` fails during startup reconciliation, the framework
logs the failure and restores open positions from the journal only.

This preserves continuity, but the restored state is provisional until broker
state can be fetched again. On the next successful periodic reconciliation,
local state is corrected back to broker truth.

---

## Periodic reconciliation

Recovery is not limited to restarts. `ReconciliationManager` also performs
periodic reconciliation every `reconcile_interval` target-period candles
(default: `4`).

During that periodic check:

- instruments with recent order completions are skipped once to avoid broker
  settlement races
- phantom local positions are cleared
- orphan or failed-exit broker positions are adopted
- size and direction mismatches are corrected
- newly adopted positions are passed through `post_warmup_check()`

If corrections were applied, the journal is persisted immediately.

---

## Strategy requirements

To participate in crash recovery, a strategy must satisfy the
`ReconcilableStrategy` protocol used by `ReconciliationManager`.

That means the strategy must be able to:

- serialize local state with `to_journal_entry(...)`
- restore prior state with `restore_from_journal(...)`
- evaluate a recovered position with `check_restored_position(...)`

Without those hooks, the framework cannot safely restore or adopt positions on
the strategy's behalf.

---

## Log messages

The reconciliation code emits stable log fragments that are useful when wiring
runtime-specific alerts.

| Level    | Message fragment                              | Meaning                                |
|----------|-----------------------------------------------|----------------------------------------|
| INFO     | `Journal loaded: N entries (M open, K flat)`  | Journal restored successfully          |
| INFO     | `No journal found; starting fresh`            | No prior persisted state               |
| INFO     | `Startup reconciliation: all N positions match` | Journal and broker agree             |
| WARNING  | `Phantom position cleared: INSTRUMENT`        | Local-only position was reset          |
| WARNING  | `Adopting orphan broker position: INSTRUMENT` | Broker-only position was adopted       |
| WARNING  | `Size corrected: INSTRUMENT`                  | Broker size replaced local size        |
| WARNING  | `Direction corrected: INSTRUMENT`             | Broker direction replaced local state  |
| CRITICAL | `FAILED EXIT DETECTED: INSTRUMENT`            | Journal says flat, broker still open   |
| WARNING  | `restoring from journal only`                 | Startup broker fetch failed            |

---

## Minimal integration sketch

```python
from tradedesk.portfolio import PortfolioRunner, PositionJournal, ReconciliationManager

journal = PositionJournal("/var/lib/my-runtime/positions.json")

runner = PortfolioRunner(
    strategies=strategies,
    policy=policy,
    default_risk_per_trade=default_risk,
)

reconciliation = ReconciliationManager(
    runner=runner,
    client=broker_client,
    journal=journal,
    target_period="MINUTE_15",
    reconcile_interval=4,
)
```

The embedding runtime is responsible for choosing the journal path, surfacing
alerts, and defining any manual operator procedure when the runtime receives a
critical reconciliation signal.

---

## Testing

See `tests/portfolio/test_crash_recovery.py` for end-to-end coverage of journal
loading, discrepancy classification, broker fallback, and post-recovery exit
checks.

---

## License

Licensed under the Apache License, Version 2.0.
See: https://www.apache.org/licenses/LICENSE-2.0

Copyright 2026 [Radius Red Ltd.](https://www.radiusred.uk) | [Contact](mailto:opensource@radiusred.uk)
