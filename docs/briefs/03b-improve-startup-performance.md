Always read and follow instructions in `CLAUDE.md` in the project root before processing the brief.

# Improve startup performance (implementation)

## Context

See the context and architectural recommendations in `03-improve-startup-performance.md`.


## Role

In this session you will take on a role described in https://github.com/radiusred/.github/doc/agent-roles/engineer.md (or the local equivalent repository clone)

## Goal

The goal of this session is to implement the architectural and design changes outlined in option **C** of `03-improve-startup-performance.md`.

The goal is successfully achieved when long, or multi-instrument backtests can be run with no appreciable increase in startup time or memory overhead compared with shorter or single-instrument runs.


## Approach

- Replace upfront loading with per-instrument lazy generators that yield candles as they are read, and replace the flat sorted `stream` list with a k-way merge across those generators.
- The key observation is that **candles within each instrument file are already sorted chronologically**. A k-way merge across `k` instruments using `heapq.merge()` will produces a fully ordered interleaved stream with O(log k) overhead per event.
- The `BacktestStreamer.run()` loop body remains unchanged — it still dispatches one event at a time — but the `stream` list and `stream.sort()` are replaced with a generator pipeline.
- For each instrument, pre-load only the first `max(warmup_bars, aggregation_window)` candles into `_history` as today — enough to service `get_historical_candles()` calls during warmup
- Stream the remainder lazily via per-instrument generators
- The k-way merge begins after warmup completes (i.e. after `SessionReadyEvent`) so that warmup data and live-stream data are never interleaved incorrectly
- Base-period aggregation remains as a transform layer in the generator chain rather than a pre-pass
  
## Output

### Files changed

**`tradedesk/execution/backtest/dukascopy.py`**
- Added `iter_dukascopy_candles()` — validates inputs eagerly then delegates to `_iter_candles()`, a private generator that yields `Candle` objects one daily file at a time. Per-day resampling for non-1MIN periods (safe for all periods that align to day boundaries: 15MIN, 1H, 4H, 1D). No `ValueError` on empty ranges; invalid `price_side` / `period` raise immediately at call time.

**`tradedesk/execution/backtest/streamer.py`**
- `CandleSeries.candles` widened from `list[Candle]` to `Iterable[Candle]` — accepts generators.
- `BacktestStreamer.run()` replaced the flat `stream` list + `stream.sort()` with `heapq.merge` over per-instrument generators. A shared `itertools.count()` acts as tiebreaker when two instruments share a timestamp, avoiding any comparison of non-orderable event objects. O(log k) per event vs O(N log N) upfront sort.
- Two module-level helpers extracted: `_candle_gen()` and `_market_gen()`. The redundant `_set_current_timestamp` call in the old stream-build loop is removed.

**`tradedesk/execution/backtest/client.py`**
- Added `BacktestClient.from_lazy_sources()` classmethod. Accepts an explicit `history` dict (warmup slice, for `get_historical_candles()`) and `candle_series` list (lazy generators, for streaming), keeping them separate so only the warmup window needs to be resident at startup.

**`tradedesk/execution/backtest/__init__.py`**
- Exported `iter_dukascopy_candles`.

**`ig_trader/session_runners.py`**
- `build_portfolio_backtest_client()`: new `warmup_bars: int | None = None` parameter. `None` preserves existing eager behaviour. When set, pre-loads the first `warmup_bars` base-period candles per instrument into `_history` (via `itertools.islice`), then creates an independent full-range generator for streaming and returns a `from_lazy_sources` client.
- `run_portfolio_backtest()`: new `warmup_bars: int | None = None` and `run_name: str | None = None` parameters. `run_name` (or a UTC timestamp fallback) is used to compute the run output directory eagerly before any data loading, so logging is fully configured — including the file handler — before the expensive candle-loading and index-building steps. The deferred `_configure_logging_on_start` / `SessionStartedEvent` handler is removed. The subscriber is passed the pre-created `run_dir` directly via the new `run_dir` parameter (see below). `warmup_bars` is forwarded to the client builder. In lazy mode, the target-period warmup history is aggregated from the warmup slice and `ledger.candle_indices` is built from a fresh full-range generator (one extra disk pass, O(N_target) memory rather than O(N_base)).

**`tradedesk/recording/subscriber.py`**
- `RecordingSubscriber.__init__` gains `run_dir: Path | None = None`. When provided it is stored directly as `_run_output_dir` (caller is responsible for creating the directory); `handle_session_started` skips directory creation and just logs the path.
- `register_recording_subscriber` gains the matching `run_dir` kwarg and passes it through.

**`ig_trader/scripts/run_portfolio.py`**
- `--name` arg description updated to document the output-directory-naming behaviour (falls back to timestamp when omitted).
- `--warmup-bars N` arg added (`dest=warmup_bars`). Enables lazy loading when set.
- Both args forwarded to `run_portfolio_backtest`.

### Tests added

- `tests/execution/backtest/test_dukascopy_cache.py`: 9 new tests for `iter_dukascopy_candles` — single day, parity with eager `read_dukascopy_candles`, empty range, invalid `price_side` / `period` raise eagerly, ask side, 15MIN resampling, missing-day skip, laziness check.
- `tests/execution/backtest/test_streamer.py` (new file, 7 tests): `CandleSeries` with a generator, single-series order, two-instrument interleaving, 5-instrument k-way sort, lazy generator consumption, `from_lazy_sources` history and streamer behaviour.

**514 tradedesk tests pass, 223 ig_trader tests pass.**

### Usage

```bash
# Named run with lazy loading (first log line appears immediately):
python -m scripts.run_portfolio \
    --target backtest \
    --from 2022-01-01 --to 2026-01-01 \
    --name my-run \
    --warmup-bars 300
```

```python
# Programmatic equivalent:
run_portfolio_backtest(
    cfg=cfg,
    raw_cfg=raw_cfg,
    specs=specs,
    log_level="INFO",
    run_name="my-run",      # output written to back_tests/my-run/
    warmup_bars=300,        # pre-load first 300 base-period candles per instrument
)
```

`warmup_bars=300` with `base_period="1MIN"` pre-loads ~300 1MIN candles per instrument into `_history` for strategy warmup. The remainder of the date range is streamed lazily. Memory stays O(k) where k is the number of instruments, rather than O(N × k).

### Design notes

- The approach is fully backward-compatible: passing `warmup_bars=None` (the default) preserves existing eager behaviour with zero code-path changes for existing callers.
- The k-way merge starts from the beginning of the date range (i.e. it does not gate on `SessionReadyEvent`). This is correct because `BacktestStreamer` runs after `SessionStartedEvent` / `SessionReadyEvent` have already fired in `BasePortfolio.run()`. Warmup (`get_historical_candles`) and streaming are already fully separate lifecycle phases.
- The warmup `_history` slice represents the start of the backtest period rather than the end (the previous eager approach seeded indicators from the final N bars of the full dataset — a subtle correctness issue for long backtests that this implementation resolves).
- Root cause of the silent startup delay: logging was deferred to a `SessionStartedEvent` handler so the subscriber could supply the timestamped directory to the file handler. This fired inside `run_portfolio()`, after all data loading and index building. Fix: compute the run directory eagerly from `run_name` (or timestamp), create it immediately, configure logging fully, then pass the directory to the subscriber via `run_dir` so it reuses rather than recreates it.

# Cost
```
Total cost:            $6.56
Total duration (API):  16m 44s
Total duration (wall): 6h 22m 51s
Total code changes:    687 lines added, 77 lines removed
Usage by model:
claude-sonnet-4-6:  68 input, 58.2k output, 5.2m cache read, 380.4k cache write ($6.44)
claude-haiku-4-5:  47 input, 5.4k output, 272.4k cache read, 49.5k cache write ($0.1160)
```
