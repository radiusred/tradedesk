# Kill-Stack

This directory documents the **public template and process only** for kill-stage
research notes.

Actual kill memos are private research artifacts. They must **not** be committed
to this public repository because they contain named strategy archetypes,
measured backtest outcomes, and kill-stage verdicts.

## Why this exists

Strategies fail for many reasons — bad signal, hostile spread, regime mismatch,
data bug, or simply parameter brittleness. Without a written record:

- the same failed archetype gets re-tested every few months, wasting quant-days
- nobody remembers *why* it was killed, only that it was
- when the binding constraint changes (e.g. a data fix, a new instrument, a
  different resolution) we miss the chance to revive a candidate that was
  killed under stale conditions

The public repo keeps the schema and operating rules so contributors understand
how the process works without exposing private research outcomes.

Private kill memos should live only in private workspaces or private
repositories such as `ig_trader`.

## What belongs in this directory

- `README.md` — this public process note
- `_template.md` — the public-safe memo template

Do not commit:

- strategy-specific kill memos
- measured Sharpe, drawdown, spread-absorption, or alpha figures
- named strategy results or pass/fail verdicts
- backtest artifact paths containing real run outputs or configs

## Structure

Private memos should use the [`_template.md`](./_template.md) layout. Filenames
should be `lowercase_snake_case` with the archetype and, optionally, the
resolution: for example `intraday_vol_breakout.md` or `donchian_15m.md`.

That naming guidance applies in private storage, not in this public repository.

## Quarterly review

Run the quarterly review against the private kill-memo store with current
spread/regime data. Strategies killed because of a stale assumption (data scale
bug, pre-fix spread model, narrow universe) are candidates for revival. Record
that in the private memo by adding a `## Revival check (YYYY-MM-DD)` section.
