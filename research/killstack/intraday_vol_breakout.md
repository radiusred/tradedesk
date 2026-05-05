# Kill memo: IntradayVolBreakout @ 15m

- **Archetype:** IntradayVolBreakout (volatility-cone breakout with cone-trail exit)
- **Resolution / bar size:** 15m
- **Instrument set:** DAX (primary); brief sanity passes on USA500, EURUSD
- **Date range tested:** 2020-01-01 .. 2026-02-28
- **Killed at stage:** Discovery (failed Stage 1 gate; targeted tuning pass also failed)
- **Date killed:** 2026-03-19
- **Quant-days spent (cumulative):** ~2.0
- **Owner at time of kill:** Quanty

## Hypothesis (as pre-registered)

After a contracting-volatility window (rolling realized vol below its 30-day
trailing 20th percentile), the next directional break of the cone should
continue for several bars before mean-reverting. Trail the position with a
cone-width stop. Pre-registered targets:

- Per-trade Sharpe ≥ 0.6 (target 0.8)
- Total annual Sharpe ≥ 1.0 net of IG spread
- Expected ~120 trades/yr on DAX, average hold ~2 hr

## What was measured

| Metric | Pre-registered target | Measured | Pass? |
| --- | --- | --- | --- |
| Per-trade Sharpe | ≥ 0.6 | 0.18 | No |
| Total Sharpe (net of spread) | ≥ 1.0 | -0.12 | No |
| Per-trade alpha vs spread (gross) | ≥ 5× | ≈ 1.0× | No |
| Spread absorption (gross → net) | < 50% | 96% | No |
| Round trips in window | ≥ 30/yr | 138/yr | Yes |
| OOS degradation vs IS | < 50% | n/a (killed at Stage 1) | — |

## Kill stage and gate breached

Failed **Discovery / Stage 1** on three of four gates simultaneously:

1. **Per-trade Sharpe gate** (≥ 0.6): measured 0.18.
2. **Net Sharpe gate** (≥ 1.0): measured -0.12.
3. **Spread-absorption gate** (< 50% of gross alpha consumed by spread):
   measured 96%.

A targeted tuning pass on the breakout threshold and cone-trail multiplier did
not produce a monotonic improvement direction — the two-strike rule applied
and the archetype was killed.

## Kill reason (root cause)

**Death by spread.** On 15m DAX bars the entry signal generated approximately
one IG spread of per-trade alpha gross. To clear costs *and* leave headroom
for slippage and signal decay we need per-trade alpha ≥ 5× spread (the
escape-diagnostic threshold used across the killstack). The signal itself is
real — the breakout has a positive expectancy gross of costs — but the
expectancy-to-cost ratio at this resolution is structurally inadequate.

This is the same binding constraint that kills most 15m breakout / vol-based
archetypes on retail-spread broker data: noise per bar is large relative to
the move that the signal predicts, so the bid-ask round-trip is most of the
edge.

## Conditions that would justify a revival check

- Move to **hourly (1H) or higher** resolution: alpha-per-bar scales roughly
  with √(bar duration), so 1H bars give ~2× per-trade alpha at the same
  spread, materially changing the alpha:spread ratio.
- A **regime classifier** that gates the strategy to a small subset of bars
  where the breakout is structurally more directional (e.g. high-ADX, high
  realized-vol relative to implied).
- A **broker change** with materially tighter spread on DAX (≥ 30%
  improvement vs IG retail).
- A **revised exit model** that captures multi-bar persistence (e.g. partial
  exit + trail) rather than the cone-trail used here. This is closer to a new
  archetype than a revival, but worth flagging.

Do **not** re-test this archetype at 15m on retail-spread DAX without one of
the above conditions being true.

## Artifacts

- Backtest config: `research/killstack/artifacts/intraday_vol_breakout/discovery.yaml` *(historical; predates the Discovery template — not strictly conformant)*
- Backtest output / report: `research/killstack/artifacts/intraday_vol_breakout/report.md`
- Equity curve / PnL plot: `research/killstack/artifacts/intraday_vol_breakout/equity.png`
- Related: see `research/killstack/README.md` for the four-stage pipeline gates.

## Re-test history

*(none yet — to be appended on revival)*
