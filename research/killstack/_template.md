# Kill memo: <archetype-name> @ <resolution>

- **Archetype:** <e.g. BollingerReversion, Donchian breakout, IntradayVolBreakout>
- **Resolution / bar size:** <15m | 1H | 4H | 1D>
- **Instrument set:** <e.g. DAX, EURUSD, USDJPY>
- **Date range tested:** <YYYY-MM-DD .. YYYY-MM-DD>
- **Killed at stage:** <Discovery | Validation | Live-paper | LIVE>
- **Date killed:** <YYYY-MM-DD>
- **Quant-days spent (cumulative):** <N>
- **Owner at time of kill:** <agent / user>

## Hypothesis (as pre-registered)

One paragraph: what edge was this strategy supposed to capture? What was the
*a priori* expected per-trade alpha, expected number of trades per year, and
minimum acceptable Sharpe?

## What was measured

Concrete numbers from the actual backtest. Always include enough that a future
reader can decide whether the kill is still binding.

| Metric | Pre-registered target | Measured | Pass? |
| --- | --- | --- | --- |
| Per-trade Sharpe | e.g. ≥ 0.6 | e.g. 0.21 | No |
| Total Sharpe (net of spread) | e.g. ≥ 1.0 | e.g. -0.18 | No |
| Spread absorption | < 50% | e.g. 93% | No |
| Round trips in window | ≥ 30 | e.g. 47 | Yes |
| OOS degradation vs IS | < 50% | e.g. n/a (killed at Stage 1) | — |
| Avg correlation vs LIVE sleeves | < 0.4 | e.g. n/a | — |

## Kill stage and gate breached

Name the exact gate from the methodology that triggered the kill. Quote the
threshold and the measured value. If multiple gates failed, list them in the
order they were checked.

## Kill reason (root cause)

One or two paragraphs explaining *why* the strategy failed, not just *that* it
failed. Examples:

- Death-by-spread: per-trade alpha was approximately 1× spread, well below the
  ≥5× ratio required to clear costs and slippage.
- Regime drift: signal worked in 2018–2020 trending regime but inverted in
  post-2022 mean-reverting regime; OOS Sharpe -0.4 vs IS 1.2.
- Parameter brittleness: best-cell Sharpe collapsed when k was perturbed by
  ±10%.
- Universe artifact: the only instruments where it worked are illiquid /
  non-tradable on our broker.

## Conditions that would justify a revival check

List the specific changes that would invalidate the kill reason. If none of
these are true, do **not** re-test.

- e.g. Data scale or fill-model bug that affected this run is fixed.
- e.g. Spread on the affected instruments tightens by ≥30% on our broker.
- e.g. A new resolution (e.g. 1H) where alpha-per-bar is structurally larger.
- e.g. A regime classifier becomes available so trades can be gated to the
  favorable regime.

## Artifacts

- Backtest config: `<path or hash>`
- Backtest output / report: `<path>`
- Equity curve / PnL plot: `<path>` (optional)
- Related notes / memos: `<path or link>`

## Re-test history

Append a new section here each time the archetype is revived and re-killed:

### Re-test (YYYY-MM-DD)

- Trigger condition: <which revival condition above became true>
- New result: <metrics>
- New verdict: <killed again | advanced to stage X>
