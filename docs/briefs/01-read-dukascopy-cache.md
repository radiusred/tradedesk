Always read and follow instructions in `CLAUDE.md` in the project root before processing the brief.

# Task Brief: Read `tradedesk-dukascopy` cache data for backtests

## Context

Data for backtests is currently supplied in the form of CSV candle files at some resolution (typically 1 minute) per instrument with the `tradedesk` library aggregating these into the periods actually required by the running strategy. This forces a one-off creation of the "chart" data per instrument for use in a backtest, where the chart file contains all the candles that will be used in the backtest for that instrument.

The `tradedesk-dukascopy` tool provides a way to export such files from its cache of market data by specifying start/end dates, instrument names and resampling period. But any method of generating the data in the required format is equally valid.

## Goal

The goal of this session is to enable `tradedesk` to read market data directly from the `tradedesk-dukascopy` cache and avoid having to create and re-create exported chart files for backtests. Once the tool has downloaded and converted data, it should be immediately possible to use it in a test. The downloader now stores daily "tick" files consisting of one line of prices for each movement. These are the files that will need to be read in order to create OHLCV candles for injection into the bus for backtest clients.

### Data Format

All cached data is stored in the following directory layout from the root of the cache-dir:

```text
SYMBOL
├── YEAR
    ├── 00
    │   ├── 01_ticks.csv.zst
    │   ├── 02_ticks.csv.zst
    │   ├── ...
    │   ├── 31_ticks.csv.zst
```
Months are `00` based; 00 = January, 11 = December. This is a Dukascopy convention. Each day within the month has a single compressed file of tick data.

File compression is done with the python `zstandard` library and this should be performant enough to decompress on the fly.

This is the data format for all decompressed dukascopy data:

```csv
ts,bid,ask,bid_vol,ask_vol
2026-01-30T00:00:02.244000+00:00,13807.7,13808.9,0.8999999761581421,1.6299999952316284
2026-01-30T00:00:04.506000+00:00,13807.9,13808.8,0.8999999761581421,0.7300000190734863
2026-01-30T00:00:04.609000+00:00,13807.9,13808.9,0.8999999761581421,1.7999999523162842
2026-01-30T00:00:04.711000+00:00,13807.7,13808.9,0.8999999761581421,0.8999999761581421
2026-01-30T00:00:10.805000+00:00,13808.0,13808.8,0.8999999761581421,0.8999999761581421
2026-01-30T00:00:11.218000+00:00,13808.1,13809.0,0.8999999761581421,2.9800000190734863
2026-01-30T00:00:12.487000+00:00,13808.0,13808.9,0.8999999761581421,4.5
2026-01-30T00:00:12.590000+00:00,13807.8,13808.9,0.8999999761581421,0.8999999761581421
2026-01-30T00:00:12.894000+00:00,13807.8,13808.7,0.8999999761581421,0.8999999761581421
```

## Approach

 - The original data format does not need to be retained in the framework for backwards compatibility. Tick files will be the only supported future format. The framework should, however, handle source files that are compressed (with `zstandard`) or not.
 - Chart files naturally stopped and started with whatever the first and last entry in them was. Running a backtest will now need two additional parameters / CLI args of `--from` and `--to` to determine this.
   - Missing data days within the cache are to be expected (for example when markets are closed).
 - Data using old formats might exist in the cache. This might include hourly files with `.bi5` extensions or differing directory structures if it was collected with older versions of the tool. If it is found, exit the backtest gracefully with an error message and advise the user to re-run the `tradedesk-dc-export` tool which will correct the cache.
 - There may be existing code in the `tradedesk-dukascopy` project that will be of use as a starting point since it already implements some of these requirements to produce charts. Use it if so (but copy, do not reference/import) as that will likely be removed from the tool once this work is complete.
 - 


### Stretch goal

The following should only be evaluated for complexity, do not write code or tests for them.

 - Optionally use a single side (default to BUY) or use both BUY and SELL prices to include spread costs. Use a CLI argument of `--side` with choices `{"buy", "sell", "both"}`
 - Update the reporting (`analysis.md` file) to show the spread costs if used

## Output

### New files

**`tradedesk/execution/backtest/dukascopy.py`** — cache reader module. Tick-reading logic copied from `tradedesk-dukascopy` (no import reference). Provides:
- `read_dukascopy_candles(cache_dir, symbol, period, date_from, date_to, *, price_side)` — reads daily tick files for the date range, skips missing days silently, detects old `.bi5` format and exits with guidance, resamples to `Candle` objects.
- Internal helpers: `_iter_days`, `_tick_paths`, `_check_old_format`, `_load_tick_rows`, `_period_to_pandas_rule`, `_ticks_to_candle_df`.

**`tests/execution/backtest/test_dukascopy_cache.py`** — 36 tests covering: day iteration, path construction, period conversion, old-format detection, compressed/uncompressed loading, missing-day skipping, resampling correctness, timestamp ordering, and `BacktestClient.from_dukascopy_cache` integration.

### Modified files

**`tradedesk/execution/backtest/client.py`** — added `from_dukascopy_cache(cache_dir, *, symbol, instrument, period, date_from, date_to, price_side)` classmethod.

**`tradedesk/execution/backtest/runner.py`** — `BacktestSpec` now takes `cache_dir`, `symbol`, `date_from`, `date_to`, `price_side` instead of `candle_csv`. `run_backtest` uses `BacktestClient.from_dukascopy_cache`.

**`pyproject.toml`** — added `zstandard>=0.25` dependency.

**`tests/execution/backtest/test_backtest_runner.py`** — updated for new `BacktestSpec` fields.

### Stretch goal assessment

`--side` (buy/sell/both) is already partially supported: `price_side` is threaded through `BacktestSpec`, `from_dukascopy_cache`, and `read_dukascopy_candles`. Exposing it as a CLI argument is a small addition. The spread-cost annotation in analysis reporting would require the reporting layer to carry the `price_side` context and annotate equity/trade output — moderate complexity, ~1–2 hours.


## Usage by Model

**claude-sonnet-4-6:**

| Type | Tokens |
| :--- | :--- |
| Input | 0.1k |
| Output | 34.7k |
| Cache Read | 6.6m |
| Cache Write | 170.3k |

**claude-haiku-4-5:**

| Type | Tokens |
| :--- | :--- |
| Input | 0.1k |
| Output | 6.4k |
| Cache Read | 300.3k |
| Cache Write | 66.6k |
