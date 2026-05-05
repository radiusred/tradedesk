# Backtest config templates

Standardized YAML config templates for each stage of the four-stage research
pipeline. Use these instead of hand-rolling a config so that:

- the **research metadata** (hypothesis, pre-registered targets, kill gates) is
  captured next to the runnable backtest config — same file, same review
- a future reader can decide whether a result advances or kills a candidate by
  comparing `measured` to `kill_gate` without re-deriving thresholds
- multi-hypothesis discipline (Bonferroni adjustment on parameter sweeps) is
  applied consistently

## Pipeline stages

| Stage | Template | Window | Pass gate (summary) | Hard kill |
| --- | --- | --- | --- | --- |
| 1. Discovery | [`discovery.template.yaml`](./discovery.template.yaml) | 2020-01-01 .. today | Per-trade Sharpe ≥ 0.6 AND total Sharpe ≥ 1.0 net of spread; spread absorption < 50%; ≥ 30 round trips | Per-trade Sharpe < 0.5 OR < 20 round trips |
| 2. Validation | [`validation.template.yaml`](./validation.template.yaml) | Full 8yr 2018–today + walk-forward 2y train / 6mo OOS | OOS Sharpe degradation < 50% vs IS; no single-year DD > 40% of allocation; avg correlation vs LIVE sleeves < 0.4 | OOS Sharpe < 0 OR DD > 40% in any year OR correlation ≥ 0.6 with existing LIVE |
| 3. Live-paper | *(no template — runs against DEMO)* | DEMO ≥ 30 days OR ≥ 10 round trips, whichever later | LIVE-vs-backtest fill PnL within ±2σ of expected | Fill PnL < -2σ OR systematic execution shortfall |
| 4. LIVE | *(production ops, not a backtest)* | — | 6-mo rolling Sharpe ≥ 0; trailing MaxDD within sleeve allocation | 6-mo Sharpe < 0 OR MaxDD breach |

Stages 1 and 2 are the ones that consume backtest configs and are templated
here. Stage 3 runs out of broker DEMO and Stage 4 is production — no template
needed.

## How to use

1. Copy the appropriate template to `configs/<archetype>_<stage>.yaml` in
   whatever runner repo you are working in (e.g. ig_trader/configs/).
2. Fill in **every field marked `# REQUIRED`** under `research:` before any
   data is touched. This is the pre-registration: it locks the hypothesis and
   pass gates *before* you see results.
3. Fill in `portfolio:` and `strategies:` per the runner's existing schema.
4. Run the backtest. Record measured values in the file's `measured:` block
   (or in the kill memo if the candidate is killed).
5. If pass gates are met, advance to the next stage by copying the next
   template and inheriting fields where useful. If the kill gate fires, file a
   kill memo in `research/killstack/`.

## Why pre-register?

The plan's §2.3 search-space discipline requires:

- a pre-committed per-trade alpha target, expected number of trades/yr, and
  minimum acceptable Sharpe **before** data is run
- Bonferroni-style threshold scaling on parameter sweeps with N > 10 cells
- universe-fixed before signal-fixed

The `research:` block in each template encodes all three. If the ex-post
result diverges from pre-registration, that is evidence about the archetype
to capture in the kill memo, **not** a license to retune until the result
matches.

## Compatibility note

The `portfolio:` and `strategies:` sub-trees are intentionally aligned with
the existing runner config schema (`instrument_map`, `portfolio.period`,
`strategies.<name>.{class, instruments, params}`) so existing scripts can
consume the file with no changes. The new `research:` block is additive and
runner-agnostic: runners that don't know about it can ignore the key, and a
linter can validate it independently.
