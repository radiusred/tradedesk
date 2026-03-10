Always read and follow instructions in `CLAUDE.md` in the project root before processing the brief.

# Amendment to `01-read-dukascopy-cache.md` - read candles instead

## Context

The changes outlined in `01-read-dukascopy-cache.md` worked as implemented but with a severe performance penalty of converting the cached tick data to 1-minute candle resolutions in memory. This penalty was paid on each backtest run.

`tradedesk-dukascopy` has now been amended to store compressed 1-minute candle files in the cache instead (2 per day, one with bid prices and one with ask prices).

## Goal

The goal of this session is to amend the code to read and use the already available 1-minute candle files from the cache, aggregating those as required for the backtest parameters.

The goal is successfully achieved when a backtest can be run using the new format for cached dukascopy data as input to the test.


## Approach

 - Amend existing code that reads the dukascopy cache to expect and require 1-minute candle files using the correct buy/sell side price.
 - Discard any code that generates 1-minute candles internally as these are already created.
 - Update any supporting code and tests to match the changes
 - Ensure all tests pass, and that the code is compliant with `mypy --strict`
 - Make specific note of any changes to the public API that scripts and calling code may need to be aware of
 - Do not be concerned with backward compatibility or API shims, breakages are acceptable
  
## Output

### Modified files

**`tradedesk/execution/backtest/dukascopy.py`** — rewired to read 1-min candle files.

Removed:
- `_tick_paths()` — replaced by `_candle_path()`
- `_load_tick_rows()` — replaced by `_load_daily_candles()`
- `_ticks_to_candle_df()` — no longer needed; candles are pre-computed in the cache

Added:
- `_candle_path(cache_dir, symbol, day, side)` — returns `{day}_{side}.csv.zst` path
- `_load_daily_candles(path)` — decompresses and returns a candle DataFrame with UTC DatetimeIndex; returns `None` on missing/corrupt file

Changed in `read_dukascopy_candles`:
- `price_side` now selects which cached file to read (`"bid"` or `"ask"`); `"mid"` is no longer supported (no mid file exists in the cache)
- Loads 1-min candle DataFrames per day, concatenates, and resamples once with `first/max/min/last/sum` aggregation — matching the `CandleAggregator` pattern
- Error message updated: `"No candle data found"` (was `"No tick data found"`)

**`tests/execution/backtest/test_dukascopy_cache.py`** — rewritten for candle format.

Removed: tests for `_tick_paths`, `_load_tick_rows`, `_ticks_to_candle_df`

Added: tests for `_candle_path`, `_load_daily_candles`, `_read_dukascopy_candles_ask_side`, `_read_dukascopy_candles_aggregates_to_15min`, `_read_dukascopy_candles_invalid_side_raises`, `test_backtest_client_from_dukascopy_cache_ask_side`

Test count: 36 (unchanged).

### Public API changes

- `_tick_paths` and `_load_tick_rows` and `_ticks_to_candle_df` are removed from the public-ish internal API. Any calling code that imported them directly will need updating.
- `read_dukascopy_candles` now raises `ValueError` for `price_side="mid"` (was silently supported).
- Error message prefix changed from `"No tick data found"` to `"No candle data found"` — any caller matching the old string will need updating.

### Result

498 tests pass. `mypy --strict` reports only the pre-existing `import-untyped` error for `pandas` (shared with `recording/report.py`; no stubs installed project-wide).

