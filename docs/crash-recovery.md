# Crash Recovery Runbook

This runbook covers how to recover the `ig_trader` portfolio process after an
unexpected crash or restart while positions are open.

---

## Background

The system uses a two-layer persistence mechanism:

1. **Position journal** (`positions.json`) — written atomically on every
   position change (open, close). Records direction, size, entry price,
   bars held, MFE, and entry ATR.

2. **Reconciliation manager** — on startup, loads the journal and compares it
   against live broker positions via `GET /positions`. The broker is the
   **source of truth**; the journal is a crash-recovery hint.

---

## Recovery Scenarios

### 1. Normal restart (broker and journal agree)

Outcome: positions are fully restored with all metadata intact.

```
Journal: EURUSD LONG size=1.0 bars_held=5 mfe=2.5
Broker:  EURUSD BUY  size=1.0
→ MATCHED — position restored from journal (preserves bars_held, mfe, entry_atr)
```

No manual action required.

---

### 2. Phantom local position (journal says open, broker says flat)

The position was closed at the broker (manually, or a fill arrived after the
crash) but the journal still shows it as open.

```
Journal: EURUSD LONG size=1.0
Broker:  (no position)
→ PHANTOM_LOCAL — local state reset to flat, journal updated
```

Outcome: strategy resets to flat. No manual action required.

---

### 3. Orphan broker position (journal empty, broker has a position)

The journal was not written before the crash (e.g. crash on entry), but the
order was accepted by the broker.

```
Journal: (no entry for EURUSD)
Broker:  EURUSD BUY size=1.0
→ ORPHAN_BROKER — position adopted into local state, post-warmup exit check runs
```

After adoption the strategy runs `check_restored_position` against the latest
candle. If an exit condition is met the position is closed immediately.

No manual action required, but review the logs for adopted positions and verify
the exit evaluation result.

---

### 4. Failed exit (EMERGENCY)

The strategy attempted to close a position (journal direction=None) but the
broker still has it open. This is the most critical scenario.

```
Journal: EURUSD direction=None (flat)
Broker:  EURUSD BUY size=1.0
→ FAILED_EXIT — CRITICAL log emitted, broker position adopted
```

**Action required:**

1. Check the log for `FAILED EXIT DETECTED` to identify the instrument.
2. Log into the IG web platform and verify the position manually.
3. If the position should be closed: close it manually via the IG platform.
4. Once flat, restart the portfolio process. The journal will reconcile to flat.

---

### 5. Size mismatch (partial fill)

The journal records the requested size but the broker filled a smaller amount.

```
Journal: EURUSD LONG size=2.0
Broker:  EURUSD BUY  size=1.5
→ SIZE_MISMATCH — local size corrected to broker size (1.5)
```

No manual action required. Exit logic re-evaluated with broker size.

---

### 6. Direction mismatch

The journal records one direction but the broker has the opposite.

```
Journal: EURUSD LONG size=1.0
Broker:  EURUSD SELL size=1.0
→ DIRECTION_MISMATCH — broker direction adopted, exit check runs
```

This is unusual and may indicate a separate manual trade on the same epic.
Review the IG platform to verify the direction before restarting.

---

### 7. Broker unreachable on startup

If the broker API is unavailable, the manager falls back to the journal alone
and restores positions without verification.

```
Broker: HTTP 500 / timeout
→ Positions restored from journal (unverified)
```

**Action required:**

1. Monitor logs for `restoring from journal only`.
2. Once the broker is reachable, the next **periodic reconciliation** will
   verify and correct state automatically (default: every 4 candles).
3. Until then, treat restored positions as tentative — do not add to them.

---

### 8. Corrupt journal

If `positions.json` is corrupt (disk error, interrupted write), the manager
logs the error, discards the journal, and falls back to broker positions.

```
Journal: unreadable (corrupt JSON)
→ load() returns None → broker positions adopted as orphans
```

No manual action required. If the broker shows no open positions, the process
starts fresh.

---

## Manual Intervention Steps

When a `FAILED_EXIT` is detected or you need to manually force a clean state:

```bash
# 1. Stop the portfolio process
pkill -f ig_trader  # or stop the container / systemd unit

# 2. Inspect the journal
cat /path/to/journal/positions.json | python3 -m json.tool

# 3. If you want to clear the journal entirely (e.g. all positions confirmed flat)
rm /path/to/journal/positions.json

# 4. Restart the process — it will reconcile against the broker
python -m ig_trader  # or start container
```

> **Warning:** Only delete the journal after confirming all positions are
> flat at the broker. Deleting while positions are open will force orphan
> adoption on restart, which triggers exit checks but may have slippage
> implications.

---

## Log Messages Reference

| Level    | Message fragment                              | Meaning                                    |
|----------|-----------------------------------------------|--------------------------------------------|
| INFO     | `Journal loaded: N entries (M open, K flat)`  | Normal startup with journal                |
| INFO     | `No journal found; starting fresh`            | First run or clean shutdown                |
| INFO     | `Startup reconciliation: all N positions match` | Clean reconciliation                     |
| WARNING  | `Phantom position cleared: INSTRUMENT`        | Closed externally; local reset to flat     |
| WARNING  | `Adopting orphan broker position: INSTRUMENT` | No journal entry; broker state adopted     |
| WARNING  | `Size corrected: INSTRUMENT`                  | Partial fill reconciled                    |
| WARNING  | `Direction corrected: INSTRUMENT`             | Direction mismatch; broker wins            |
| CRITICAL | `FAILED EXIT DETECTED: INSTRUMENT`            | **Manual intervention required**           |
| WARNING  | `restoring from journal only`                 | Broker unavailable at startup              |
| WARNING  | `Heartbeat Alert: no updates for N in Xs`     | Lightstreamer connection may be stale      |

---

## Periodic Reconciliation

Even during normal operation, the manager reconciles every `reconcile_interval`
target-period candles (default: 4). This catches any drift that occurs during
live trading without requiring a restart.

Instruments with **recent position changes** are excluded from a single
periodic check to avoid false positives during the broker settlement window
(e.g. a fill that was just submitted but not yet reflected in `GET /positions`).

---

## Testing

See `tests/portfolio/test_crash_recovery.py` for end-to-end crash simulation
tests covering all the scenarios described above.
