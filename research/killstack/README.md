# Kill-Stack

A durable record of strategy archetypes (or specific configurations) that have
been **killed** during research, with enough detail that we do not re-test them
for the same wrong reason and can revive them later if the binding constraint
changes.

## Why this exists

Strategies fail for many reasons — bad signal, hostile spread, regime mismatch,
data bug, or simply parameter brittleness. Without a written record:

- the same failed archetype gets re-tested every few months, wasting quant-days
- nobody remembers *why* it was killed, only that it was
- when the binding constraint changes (e.g. a data fix, a new instrument, a
  different resolution) we miss the chance to revive a candidate that was
  killed under stale conditions

Each kill memo captures the archetype, the resolution at which it died, the
exact reason it died, and any specific values (Sharpe, spread absorption, OOS
degradation) that future researchers should compare against before declaring it
dead a second time.

## When to file a memo

File a kill memo whenever a candidate is killed at any stage of the four-stage
pipeline (Discovery → Validation → Live-paper → LIVE). One memo per killed
archetype × resolution. If the same archetype is later revived and re-killed
under different conditions, append a new "Re-test" section rather than starting
a fresh file.

## Structure

Each memo is a Markdown file using the [`_template.md`](./_template.md) layout.
Filenames are `lowercase_snake_case` with the archetype and (optionally) the
resolution: e.g. `intraday_vol_breakout.md`, `donchian_15m.md`.

## Quarterly review

Once per quarter, re-read this directory with current spread/regime data.
Strategies killed because of a stale assumption (data scale bug, pre-fix spread
model, narrow universe) are candidates for revival. Mark such candidates by
adding a `## Revival check (YYYY-MM-DD)` section to the memo.
