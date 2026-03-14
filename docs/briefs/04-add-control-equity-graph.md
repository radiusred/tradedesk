Always read and follow instructions in `CLAUDE.md` in the project root before processing the brief.

# Add control line to equity graph on analysis report

## Context

`tradedesk/recording/report.py` generates a human readable report after each backtest and includes a number of generated charts. One of these charts shows the equity curve built up from the `equity_daily.csv` file (lines 715-791 of `report.py`).

## Role

In this session you will take on a role described in https://github.com/radiusred/.github/doc/agent-roles/engineer.md (or the local equivalent repository clone)

## Goal

The goal of this session is to enhance the equity curve chart by adding a second line showing a control instrument for comparison. This instrument will be the FTSE 100.

The goal is successfully achieved when the chart shows the equity curve from the backtest in the current colour (or any strong colour) and on the same axis, with the same scale, a line showing the equivalent FTSE performance in a muted grey (hex colour #888).


## Approach

 - For the same period (`--from` and `--to`) as the backtest, calculate the daily increment/decrement of the FTSE
   - this is a normalised line that always starts at 0 (zero) on the `--from` date of the backtest.
   - each subsequent day is the difference between the current day's close price and the previous day's close.
   - the net effect is an equity curve that models what would have happened if a LONG position of size 1.0 had been opened on day 1 of the test and closed on the final day of the test.
 - plot this series on the same chart that is generated for the portfolio's equity_daily series
 - use chart data available in the local dukascopy cache (`--cache-dir`) with an instrument name of `GBRIDXGBP`
  
## Output

Four files modified across two projects:

**`tradedesk`:**

- `tradedesk/execution/backtest/__init__.py` — exported `read_dukascopy_candles`
- `tradedesk/recording/report.py` — added `cache_dir: Path | None = None` to `_prepare_graphs` and `generate_analysis_report`; equity curve section now loads GBRIDXGBP daily candles when `cache_dir` is provided, computes a normalised cumulative series starting at 0, and plots it as a second line in `#888888`
- `tradedesk/recording/subscriber.py` — added `cache_dir` parameter to `RecordingSubscriber.__init__` and `register_recording_subscriber`; passes it through to `generate_analysis_report`

**`ig_trader`:**

- `ig_trader/session_runners.py` — passes `cache_dir=cfg.cache_dir` to `register_recording_subscriber` in the backtest runner

**Logic:** daily close prices are resampled from GBRIDXGBP 1-minute Dukascopy data; the control series is `[0, Δclose₁, Δclose₁+Δclose₂, ...]`, equivalent to holding a size-1 long position from the first day of the backtest. The date range is derived from the equity curve's own timestamps so no additional parameters are needed at the call site. Failures silently warn and omit the control line rather than crashing the report.
