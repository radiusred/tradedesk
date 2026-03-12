Always read and follow instructions in `CLAUDE.md` in the project root before processing the brief.

# Improve backtest startup performance

## Context

Backtests work by reading 1-minute OHLCV candle files, aggregating them as necessary, and using them as the source of streaming prices to test strategies. Currently these candles are all read into memory priot to the test starting.

In `tradedesk` applications, a portfolio can consist of several instruments and potentially long time periods causing an issue of delayed start (with no user feedback because logging has not been initiated at this point) and an inefficient use of memory.

## Role

In this session you will take on a role described in https://github.com/radiusred/.github/doc/agent-roles/consultant-architect.md (or the local repository clone)

## Goal

The goal of this session is to improve both startup time and memory efficiency of backtesting. With an event driven system, the aim should be to ideally inject candles into the event bus as they are available.

The goal is successfully achieved when long, multi-instrument backtests can be run with no appreciable increase in startup time or memory overhead compared with shorter or single-instrument runs.


## Approach

 - Evaluate (do NOT write/edit code) two approaches to data loading.
   - Should all the candles still be loaded into memory before starting, in which case multi-thread the load (one thread per instrument) before finally sorting by date and aggregating instruments into the current single candle list. This may solve the time issue even if the memory problem is not addressed. However, it may solve neither if the multi-threaded approach blocks on disk IO.
   - Can injection of candles into the "bus" begin as soon as they are read - effectively implementing a real stream with the disk cache as the source doing what a broker's streaming client would do. This is more idealised, solves both problems but could be more complex than it's worth.

Outline pros and cons of each approach, plus any other you feel worthy of consideration. Agree the way forward, but do NOT implement anything at this stage, implementation will be in a later brief.
  
## Output

### Architecture Observations

There are two distinct memory allocations to account for, both of which are problems:

1. **Load phase** (`build_portfolio_backtest_client`): Reads all CSV files into `BacktestClient._history` — a `dict[(epic, period) → list[Candle]]`. This happens *before* `asyncio.run()`, so it is synchronous, sequential, and unlogged.

2. **Stream build phase** (`BacktestStreamer.run()`): Iterates every loaded candle, constructs a `CandleClosedEvent` object per candle, appends to a flat `stream` list, then calls `list.sort()`. This is a *second full materialisation* of the entire dataset, doubling the memory footprint before a single event is dispatched.

For a 4-year, 5-instrument backtest at 1MIN resolution, each phase is on the order of several million objects. The sort in phase 2 is also O(N log N) across the full merged set, adding non-trivial CPU time before the replay begins.

Any solution must address **both phases** to be effective.

---

### Approach A: Parallel Loading (Multi-threaded)

Load each instrument's CSV concurrently via a `ThreadPoolExecutor`, then proceed as now.

**Pros**
- Minimal architectural change — only `build_portfolio_backtest_client` is touched
- Reading zstd-compressed CSV is largely IO-bound; threads bypass the GIL for that portion
- Proportional speedup with instrument count for the load phase
- Low risk: downstream code (BacktestClient, BacktestStreamer, strategies) unchanged

**Cons**
- Addresses startup time only, not memory — all candles remain fully resident in `_history` and then again in `stream`
- Pandas resampling (1MIN → target) is CPU-bound; GIL limits real parallelism there, so the improvement may be partial
- If instruments share a single storage device, concurrent reads may contend rather than parallelise (less relevant with SSD)
- `BacktestStreamer.run()` still builds and sorts the full `stream` list — this phase dominates at scale and is untouched
- The problem gets worse linearly with scale; this approach just defers it

**Verdict**: Partial, short-term fix. Solves neither the fundamental memory problem nor the stream-build bottleneck. Not recommended as the primary strategy.

---

### Approach B: Streaming Injection (Lazy/Generator Pipeline)

Replace upfront loading with per-instrument lazy generators that yield candles as they are read, and replace the flat sorted `stream` list with a k-way merge across those generators.

The key observation is that **candles within each instrument file are already sorted chronologically**. A k-way merge across `k` instruments using `heapq.merge()` produces a fully ordered interleaved stream with O(log k) overhead per event — no upfront sort required, and only a constant-size heap in memory at any point.

The `BacktestStreamer.run()` loop body would be unchanged — it still dispatches one event at a time — but the `stream` list and `stream.sort()` are replaced with a generator pipeline.

**Pros**
- Solves both problems: startup begins immediately (no pre-load phase); memory per in-flight event is O(1) rather than O(total candles)
- No architectural change to the event dispatch path or strategies
- Scales arbitrarily — a 10-year, 20-instrument backtest uses no more memory than a 1-day, 1-instrument run
- Aligns with the system's stated design intent: injecting events as they arrive, as a real broker feed would
- `heapq.merge()` is already available in the standard library; no new dependencies

**Cons**
- **Warmup mechanism is broken by this approach.** `get_historical_candles(epic, period, n)` reads from `_history`, which would no longer be pre-populated. Strategies that call this during warmup (to seed indicators before live replay) would fail.
  - This is the dominant architectural challenge.
  - MacroTrendStrategy already works around this by gating on `_bar_count >= period` rather than using warmup — that pattern generalises, but it requires strategy-level changes.
  - An alternative: pre-load only the warmup window (first `warmup_bars` worth of data per instrument) into `_history`, then stream the remainder lazily. This is a small fraction of the total data.
- The base-period pre-aggregation step in `run_portfolio_backtest` also populates `_history[(epic, target_period)]` — this would need rethinking, though it could be done inline in the generator chain.
- Slightly more complex streaming code; ordering bugs would be harder to detect than with a sort-then-replay approach.

**Verdict**: The right long-term architecture. The warmup issue is the only substantive obstacle and has a clear resolution path.

---

### Approach C (Additional Consideration): Lazy Pipeline with Partial Warmup Pre-load

A refinement of Approach B that resolves the warmup issue cleanly:

- For each instrument, pre-load only the first `max(warmup_bars, aggregation_window)` candles into `_history` as today — enough to service `get_historical_candles()` calls during warmup
- Stream the remainder lazily via per-instrument generators
- The k-way merge begins after warmup completes (i.e. after `SessionReadyEvent`) so that warmup data and live-stream data are never interleaved incorrectly
- Base-period aggregation remains as a transform layer in the generator chain rather than a pre-pass

This preserves full backward compatibility with existing warmup-dependent strategies while gaining the memory and startup benefits of lazy streaming. It is the pragmatic middle path: no strategy changes required, no warmup regressions, full scalability.

---

### Recommendation

**Approach C** is the recommended path.

Approach A offers a small, bounded startup improvement but leaves the fundamental problems untouched — it is not worth the ongoing complexity budget given it will need to be revisited.

Approach B/C together represent a clean, principled solution that aligns with the event-driven design of the system. The implementation surface is well-contained:

- `BacktestStreamer`: replace `stream` list + sort with k-way merge generator
- `BacktestClient` / `dukascopy.py`: expose a lazy candle generator alongside (or replacing) the current eager loader
- `build_portfolio_backtest_client` / `run_portfolio_backtest`: pre-load warmup slice only; remove full upfront load
- Strategies: no changes required if partial pre-load is used

The warmup boundary (when lazy streaming begins) maps naturally onto the existing `SessionStartedEvent` → `SessionReadyEvent` lifecycle, which already separates warmup from live replay.

One recommendation for the implementation brief: define a `CandleSource` protocol (or similar) that both the eager `list[Candle]` and the lazy generator can satisfy. This keeps `BacktestStreamer` decoupled from the loading mechanism and avoids divergent code paths for tests versus production backtests.


# Cost
```
Total cost:            $0.4956
Total duration (API):  4m 25s
Total duration (wall): 12h 25m 16s
Total code changes:    91 lines added, 1 line removed

Usage by model:
claude-sonnet-4-6:  9 input, 5.7k output, 118.9k cache read, 21.2k cache write ($0.3335)
claude-haiku-4-5:  107 input, 6.9k output, 599.1k cache read, 54.1k cache write ($0.1621)
```
