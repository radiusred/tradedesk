# Research methodology tooling

The `tradedesk.research` package automates two manual steps in the
candidate-strategy workflow:

1. **Walk-forward harness** — runs a 2yr-train / 6mo-OOS rolling loop and
   reports IS Sharpe, OOS Sharpe, OOS/IS degradation ratio, and per-year
   drawdown on the chained OOS curve.
2. **Correlation gate** — given a candidate's daily PnL and the daily PnL of
   the existing LIVE sleeves, computes the pairwise Pearson correlation
   matrix and flags any sleeve at or above the 0.6 hard-kill threshold.

Both tools sit on top of the existing event-driven backtest runner; no
new market-data plumbing is introduced.

## Walk-forward harness

```python
import asyncio
from datetime import date
from pathlib import Path
from tradedesk.research import WalkForwardSpec, run_walk_forward

def factory(client, window, phase):
    # phase is "train" or "oos" — fit on train, freeze on oos
    return build_my_strategy(client, fit_window=window if phase == "train" else None)

spec = WalkForwardSpec(
    portfolio_factory=factory,
    instrument="EURUSD",
    period="HOUR",
    cache_dir=Path("/paperclip/tradedesk/marketdata"),
    symbol="EURUSD",
    date_from=date(2018, 1, 1),
    date_to=date(2024, 1, 1),
)
report = asyncio.run(run_walk_forward(spec=spec, out_dir=Path("/tmp/wf")))
print(report.is_sharpe, report.oos_sharpe, report.degradation_ratio)
```

Window cadence is configurable via `train_window` and `oos_window`
(defaults: 730 days train, 183 days OOS). Windows step forward by
`oos_window` so the OOS slices are contiguous — you get continuous
coverage for the chained drawdown calculation.

When `out_dir` is supplied, every window writes its IS and OOS run
artefacts under `{out_dir}/{train_from}_{oos_to}/{is|oos}/` for later
inspection.

End-to-end script: [docs/examples/research_walk_forward.py](examples/research_walk_forward.py).

## Correlation gate

```python
from tradedesk.research import correlation_gate, daily_pnl_from_csv

candidate_pnl = daily_pnl_from_csv("trades_candidate.csv")
sleeves = {
    "fx_meanrev": daily_pnl_from_csv("live/fx_meanrev.csv"),
    "dax_breakout": daily_pnl_from_csv("live/dax_breakout.csv"),
}
result = correlation_gate("my_candidate", candidate_pnl, sleeves)
if result.fails_gate:
    raise SystemExit(f"correlation kill: {result.flagged}")
```

The gate accepts daily PnL as a `dict[date, float]`. Helpers exist for the
three input shapes researchers actually have to hand:

| Source | Helper |
|--------|--------|
| Reconstructed `RoundTrip` objects from a backtest | `daily_pnl_from_round_trips` |
| Raw fill-row dicts | `daily_pnl_from_trade_rows` |
| A trade-log CSV on disk | `daily_pnl_from_csv` |

Days where either series is silent are dropped, not zero-filled — silent
days carry no information about co-movement and would dilute the
correlation. Sleeves with fewer than `min_overlap_days` (default 20) of
shared trading days are reported in `skipped_sleeves` and excluded from
the kill check.

End-to-end script: [docs/examples/research_correlation_gate.py](examples/research_correlation_gate.py).

## Research pipeline infrastructure

### Four-stage pipeline

The `tradedesk.research` API tools fit inside a four-stage vetting
pipeline. Each stage has explicit pass gates and a hard-kill threshold;
candidates that miss any kill gate are filed in the kill-stack (see
below) and not retested for the same reason.

| Stage | Window | Pass gate (summary) | Hard kill |
|-------|--------|---------------------|-----------|
| 1. Discovery | 2020-01-01 → today | Per-trade Sharpe ≥ 0.6 AND total Sharpe ≥ 1.0 net of spread; spread absorption < 50%; ≥ 30 round trips | Per-trade Sharpe < 0.5 OR < 20 round trips |
| 2. Validation | Full 8yr 2018–today + walk-forward 2yr train / 6mo OOS | OOS Sharpe degradation < 50% vs IS; no single-year DD > 40% of allocation; avg correlation vs LIVE sleeves < 0.4 | OOS Sharpe < 0 OR DD > 40% in any year OR correlation ≥ 0.6 with existing LIVE |
| 3. Live-paper | DEMO ≥ 30 days OR ≥ 10 round trips | LIVE-vs-backtest fill PnL within ±2σ of expected | Fill PnL < −2σ OR systematic execution shortfall |
| 4. LIVE | 6-mo rolling | 6-mo Sharpe ≥ 0; trailing MaxDD within sleeve allocation | 6-mo Sharpe < 0 OR MaxDD breach |

Stages 1 and 2 consume backtest configs (see templates below). Stages 3
and 4 run against the broker.

### Backtest config templates

Stage-gated YAML templates for Stages 1 and 2 are maintained privately
(outside this public repo) in `ig_trader/configs/`:

- `discovery.template.yaml` — Stage 1 discovery run
- `validation.template.yaml` — Stage 2 validation run with walk-forward

Usage:

1. Copy the appropriate template to `configs/<archetype>_<stage>.yaml` in
   the runner repo (`ig_trader/configs/`).
2. Fill in every field marked `# REQUIRED` under the `research:` block
   **before** any data is touched. This is the pre-registration: it locks
   the hypothesis and pass gates before results are visible.
3. Fill in `portfolio:` and `strategies:` per the runner's existing schema.
4. Run the backtest. Record measured values in the `measured:` block.
5. If pass gates are met, copy the next template. If a kill gate fires, file a
   kill memo in the private kill-stack (see below).

The `research:` block is additive; runners that do not recognise it can
ignore it. A separate linter can validate the block independently.

### Kill-stack

Archetypes that have been killed at any stage are recorded as Markdown
memos in the **private** `ig_trader` repo (under `research/killstack/`).
Strategy results and pass/fail figures must never be committed to this
public repo (see RAD-1022). Each memo captures:

- the archetype and resolution at which it died
- the exact kill reason (Sharpe, spread absorption, OOS degradation, or
  correlation) with measured values
- any specific conditions that would permit a future revival

**When to file:** immediately after a kill gate fires. One memo per killed
archetype × resolution. If the same archetype is later revived and re-killed
under different conditions, append a new "Re-test" section rather than starting
a fresh file.

**Quarterly review:** re-read the kill-stack with current spread and regime
data. Candidates killed under stale conditions (data bug, pre-fix spread model,
narrow universe) are tagged with a `## Revival check (YYYY-MM-DD)` section for
the next discovery run.
