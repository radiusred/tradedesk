# tradedesk-backtest

The tradedesk backtester. It replays historical 1-minute bid/ask bars for strategy
research only. **There is no live trading here**, and no broker connectivity or order
routing. It reads market data through `tradedesk-data`'s `Reader` and aggregator.

**No strategy ships in this crate.** It provides the `Strategy` trait strategies
implement and a `StrategyRegistry` they register in; the strategies themselves live in
the crates that own them. It was cut from tradedesk-miner's `miner-backtest` without
its strategy implementations, their goldens and their parity harness.

## Scope

- **Data layer.** This crate reads bid and ask 1-minute bars through any
  `tradedesk_data::Reader` and joins them into one bid/ask series. It keeps the
  1-minute bars and aggregates them to 5m, 15m, 1h and 1d. Each instrument's
  window is read once per run into memory that parallel sweep cells borrow.
- **Execution layer.** It prices fills under an explicit per-venue,
  per-instrument cost model and charges overnight financing on the venue's
  rollover calendar. Stops and targets are evaluated under a per-run mode.
  Everything is booked in a `Ledger` that the metrics read.
- **Metrics.** `Metrics::compute` turns a finished ledger into a daily
  marked-to-market equity curve, the annualised Sharpe from daily returns, the
  maximum drawdown in pounds and in percent, and trade statistics.
- **Indicators and the strategy trait.** It provides streaming indicators,
  pinned to golden fixtures generated from tradedesk's Python implementations,
  and a `Strategy` trait that emits signals.
- **Engine and sweeps.** `engine::run` drives one strategy over loaded bars into
  a ledger and its metrics. `sweep::run_sweep` runs a parameter grid of a
  registered strategy under rayon over bars loaded once, records every trial in
  a JSON Lines registry, and reports the deflated Sharpe, the probability of
  backtest overfitting and a rolling walk-forward across the sweep.
- **Command line.** `tradedesk_backtest::cli` runs a sweep spec into a registry
  and reports on a sweep, with the miner's stdout/stderr discipline (see
  "Command line"). The `tradedesk-backtest` binary is that command line with no
  strategy registered.

## Public types

| Type | What it is |
|---|---|
| `Ohlcv` | One side's open, high, low, close and summed tick volume for a bar. |
| `JoinedBar` | Bid and ask `Ohlcv` for one bar, keyed by its UTC open. Both sides are always present. |
| `BarTimeframe` | `M1` (passthrough), `M5`, `M15`, `H1`, `D1`. `bucket_open` floors a timestamp to the timeframe. |
| `JoinedSeries` | An immutable series of joined bars at one timeframe. Opens are strictly ascending and aligned to the timeframe. |
| `OneSidedMinute` | A minute present on one side only, with the side that has it. |
| `join_sides` | Merges the bid and ask `RawBar` streams on the bar open. |
| `aggregate_series` | Turns a 1m `JoinedSeries` into a `JoinedSeries` at any timeframe. It wraps tradedesk-data's `aggregate`. |
| `LoadRequest` | A symbol, a half-open UTC window and the timeframes to build. `utc_days` covers whole inclusive days. `with_minute_costs(MinuteLookback)` also builds the instrument's `MinuteCosts`. |
| `OneSidedPolicy` | `Reject` (the default) fails the load on any one-sided minute. `Report` keeps the two-sided bars and attaches the list. |
| `load` → `MarketData` / `InstrumentSeries` | Reads each `(symbol, side)` once, in parallel across instruments, and returns read-only `Send + Sync` data. |
| `MinuteCosts` / `MinuteLookback` | One instrument's per-minute ranges and ask − bid over the window and a look-back before it, which the volatility slippage and the spread excess read (`InstrumentSeries::minute_costs`). |
| `CostConfig` | The validated cost configuration. `builtin()` loads the checked-in `config/costs.toml`, a labelled example with illustrative figures, and `from_path` / `from_toml_str` load a real file. Per venue, `placeholders` lists every unverified component, `exclusions` every excluded one, `estimates` every estimated one, `minimum_spreads` the instruments whose spread is a published minimum, `costed` which instruments can be reported as costed, and `cost_models` each instrument's slippage model and spread excess. |
| `InstrumentSpec` | Venue-independent units: `asset_class`, `quote_currency`, `raw_scale` (raw → quote), `pip_size`, and `divergence_tolerance`. |
| `Venue` / `VenueInstrumentCost` | A venue's rollover clock, weekend rules and per-instrument `price_side`, `Spread`, `Slippage` (`SlippageModel` fixed or volatility), `Commission`, `Financing`, `Carry`, an optional `SpreadExcess` and an optional `Dividend` (its ex-date `DividendTable` of `DividendRow`s). Each component (`CostComponent`) has its own `source` and a `ProvenanceStatus`: verified, placeholder, excluded or estimated (`provenance()`, `is_costed()`). |
| `Spread` / `SpreadBasis` / `SpreadWindow` | The venue spread: a flat `pips`, or a time-of-day `schedule` of local windows in `tz` (`pips_at(instant)`), with a `basis` of `average` or `minimum`. |
| `Exclusion` / `ExclusionEffect` | An excluded component in a run's metadata, with its reason and whether results stay costed with the cost `omitted` or are `uncosted`. |
| `FillModel` | One venue × instrument. `quote(bar, PricePoint, at, TradeSide, size)` prices a market fill at instant `at` as a `FillQuote`, and `quote_as(…, FillKind)` a fill of any kind (`market`, `stop`, `limit`). `attach_minutes` gives it the `MinuteCosts` the volatility models read; the quote's `FillCostDetail` records what they used. `reference(bar)` gives the configured side's OHLC, and `mark_close(bar)` gives the mark. |
| `PricePoint` | `Open`, `Close` or `Level(raw)`: where in the bar a fill happens. |
| `FillError` | `Divergence` (the bar's ask − bid is beyond tolerance), `InvalidSize`, `NonFinite`, `NoVolatility` (a volatility slippage with no minute to read) or `NoMinuteData` (no `MinuteCosts` attached). A fill error is never resolved by a fallback. |
| `RolloverCalendar` / `Rollover` | A venue's local rollover instants between two UTC times, each with the days it charges. |
| `ExitEvaluation` / `BothHit` | `Intrabar { both_hit: StopFirst \| Conservative }` or `CloseOnly`. `evaluate` returns an `ExitTrigger` with an `ExitReason` and a `PricePoint`. |
| `Ledger` | The book for one run at one venue: `open`, `check_exit`, `set_levels`, `close`, `close_signal` (a strategy exit, booked as `Signal` with its `SignalExitReason` label), `accrue_financing` and `mark_to_market`, plus read access to `fills`, `financing_charges`, `open_positions` and `closed_trades`. |
| `Fill` / `ClosedTrade` / `OpenPosition` / `FinancingCharge` / `PositionMark` | The ledger's records. All of them are `Serialize`. |
| `RunMetadata` | The venue, product, `ExitEvaluation` and `AccountFx` of a run, and its venue's cost provenance: `placeholders`, `exclusions`, `estimates`, `minimum_spreads`, `cost_models` and the derived per-instrument `costed` map (`is_costed(instrument)`), recorded with its results, and `end_of_run` (an `EndOfRunRule`: the `EndOfRun` mode and the `FinalBarEntry` rule) when the engine drove the run. |
| `AccountFx` | Fixed quote → GBP rates for a run. `units_for_stake_per_point` converts a spread-bet stake to instrument units. |
| `MarkSource` | Where marks come from. `MarketData` provides them from the 1m series, and a `JoinedSeries` from its own bars. |
| `MetricsConfig` | The run window (inclusive UTC days), the starting capital in the account currency, and the annual risk-free rate (default 0). |
| `Metrics` | Everything the metrics report for one run, `Serialize + Deserialize`: run metadata, conventions, return statistics, drawdowns, trade statistics, open positions at the end and the equity curve. `daily_returns()` gives the series behind the Sharpe. |
| `MetricsError` | An invalid config, a window with no weekday, a fill outside the window, or a position that cannot be marked (`LedgerError`). |
| `EquityPoint` | One close: equity, realised and unrealised net, open exit cost, cumulative gross P&L and costs, and the simple and log return. Its `kind` (`PointKind`) is `weekday`, or `weekend_close` for the extra last point of a window that ends on a Saturday or Sunday. |
| `ReturnStats` | Sharpe (annualised and per observation), the observation count, mean, std, skewness and kurtosis; volatility and Sortino as secondary figures. |
| `MaxDrawdown` / `Drawdown` | The deepest loss `by_amount` (account currency) and `by_fraction` (of the running peak), each with peak, trough and recovery dates. |
| `TradeStats` | Win rate, average win and loss, expectancy, profit factor, holding time, streaks, the exit-reason breakdown and the cost drag over closed trades. |
| `OpenAtEnd` / `OpenPositionValue` | Positions still open when the window closes, valued at liquidation. They are never counted as closed trades. |
| `Conventions` | The day convention, mark convention, periods per year, std ddof and risk-free rate, written with every result. |
| `engine::run` / `EngineConfig` / `SizingPolicy` / `EndOfRun` / `EndOfRunRule` / `FinalBarEntry` / `EngineStats` | One strategy run from loaded bars to `Metrics` (see "The engine"). |
| `strategy::Strategy` / `Signal` / `PositionFeedback` / `BarFeed` / `run_signals` | The trait a strategy implements, its signals and the engine's feedback (see "The strategy trait"). |
| `sweep::StrategyRegistry` / `StrategyFactory` | The strategies a sweep can run, each a factory registered under the name a spec uses (see "Registering a strategy"). |
| `sweep::SweepSpec` / `run_sweep` / `run_sweep_cancellable` / `run_sweep_on` / `validate_spec` | A parameter sweep under rayon over bars loaded once, optionally stopped by a cancel flag (see "Sweeps and the trial registry"). |
| `sweep::RegistryRecord` / `JsonlRegistry` / `read_registry` | The trial registry: append-only JSON Lines, one atomic append per record, read back losslessly; a truncated tail or an unknown `schema_version` is refused. |
| `sweep::SweepReport` / `sweep::dsr` / `sweep::cscv` / `sweep::WalkForwardReport` | The deflated Sharpe, the PBO and the walk-forward across a sweep. |
| `cli::main` / `cli::run` | The command line, given the strategies `sweep` can run (see "Command line"). |

## Data rules

- **Units.** Prices stay in the cache's raw units: EURUSD `11536.0` is
  1.15360, XAUUSD is in cents and indices are in points. Conversion to quote
  units belongs to instrument configuration.
- **Bucketing.** Buckets are UTC-aligned and labelled with the bucket open.
  Daily bars run from `00:00Z` to `00:00Z`, which matches the Python reference. For FX and metals this produces a
  short Sunday bar from the Sunday-evening open to midnight. A bucket with no
  minutes is omitted, never interpolated.
- **One-sided minutes.** A minute missing from either side is never
  zero-filled, forward-filled or dropped silently. `OneSidedPolicy` decides
  whether such minutes fail the load or are reported alongside the series.
- **Corrupt prices.** Every joined minute's open, high, low and close, on both
  sides, must be finite and above zero. Anything else fails the load with
  `LoadError::BadPrice` (symbol, minute, side, field, value) instead of
  leaving a hole in the P&L. The ledger refuses a financing or valuation mark
  that is not a finite price above zero (`LedgerError::BadMark`), and books
  nothing, for marks that do not come from the loader.

## Costs and financing

### Config schema (`config/costs.toml`)

The checked-in `config/costs.toml` is a **labelled example**. Its venue
figures are illustrative: none is any real venue's, and none may be used for
costed results. It shows the schema and exercises every feature of it, and the
crate's tests use it. A real run loads its own file with the same schema:
`tradedesk-backtest sweep --cost-config <PATH>` (see "Command line"),
`cost_config` in the sweep spec, or `CostConfig::from_path`. Without one, the
example is used.

```toml
[instruments.EURUSD]
asset_class = "fx"              # fx | metal | index | energy: selects the weekend rule
quote_currency = "USD"
raw_scale = 0.0001              # quote price = raw cache price x raw_scale
pip_size = 0.0001               # one pip/point in quote price
divergence_tolerance = 100.0    # max bar |ask - bid| in pips/points a fill accepts

[venues.example_spread_bet]
name = "Example spread bet (illustrative figures)"
product = "spread_bet"          # spread_bet | cfd
rollover = { tz = "Europe/London", time = "22:00" }
weekend.fx = { triple_day = "wednesday", days_on_triple = 3 }
weekend.metal = { triple_day = "friday" }       # days_on_triple defaults to 3
weekend.index = { triple_day = "friday" }
weekend.energy = { triple_day = "friday" }  # optional: required if the venue costs an energy instrument

[venues.example_spread_bet.instruments.EURUSD]
price_side = "mid"              # bid | ask | mid
# Every component carries its own provenance; all five are required.
spread = { pips = 1.0, basis = "average", placeholder = false, source = "..." }  # full width
slippage = { placeholder = false, excluded = true, reason = "...", source = "..." }
commission = { per_fill = 0.0, per_unit_per_fill = 0.0, per_round_trip = 0.0, placeholder = false, source = "..." }  # account currency
financing = { long_rate = 0.02, short_rate = 0.02, day_count = 360, placeholder = false, source = "..." }
carry = { placeholder = false, excluded = true, reason = "...", source = "..." }
# or modelled from a dated rate table (see "Carry" below):
# carry = { placeholder = false, estimated = true, table = "carry/eurusd.csv", sha256 = "...", day_count = 365, source = "..." }
# Optional: absent charges nothing. Charged from an ex-date table (see "Dividend" below):
# dividend = { placeholder = false, estimated = true, table = "dividend/x.csv", sha256 = "...", tz = "America/New_York", time = "16:00", weekdays_before = 1, source = "..." }

[venues.example_spread_bet.instruments.USA500IDXUSD.spread]   # a spread by time of day
pips = 1.0                      # outside every window
basis = "minimum"
tz = "Europe/London"            # the windows' local time zone
schedule = [
    { from = "14:30", to = "21:00", pips = 0.5 },   # [from, to); to < from wraps midnight
    { from = "21:00", to = "23:00", pips = 2.0 },
]
placeholder = false
source = "..."
```

Unknown fields are load errors. Every load is validated: costs must be finite
and non-negative, the zone and time must parse, `fx`, `metal` and `index`
each need a weekend rule, `energy` needs one in every venue that costs an
energy instrument (a config without energy instruments needs no
`weekend.energy`), every `days_on_triple` must be at least 1, `day_count`
must be 360 or 365, every venue instrument must be defined under
`[instruments]`, and every component must have a non-empty `source`. The amounts inside `slippage` and `commission` default to zero; the
blocks themselves and their provenance do not, so a zero is always stated
with a source.

**Provenance.** Each component is in exactly one of four states:

- **verified** (`placeholder = false`): a figure the venue publishes. The
  `source` names the page, its URL and the date it was read. (In the builtin
  example every `source` says the figure is an example instead.)
- **placeholder** (`placeholder = true`): an assumption or a stand-in. A run
  that uses one is never costed. The builtin example has none.
- **excluded** (`excluded = true` with a `reason`): the venue publishes no
  figure. The component charges nothing, so every amount in it must be zero,
  and an excluded spread has no `basis` or schedule. What that does to a
  result depends on the component (`CostComponent::exclusion_effect`):
  - An excluded **spread** or **commission** makes the venue × instrument
    **uncosted**. It still runs, at no spread or commission, so it can be used
    for signal work, but it is never reported as costed.
  - Excluded **slippage**, **spread excess**, **financing**, **carry** or
    **dividend** is **omitted**. The result stays costed and lists what it
    leaves out.
- **estimated** (`estimated = true`, on `slippage`, `spread_excess`, `carry`
  and `dividend` only):
  the figures are derived from data, not published. The component is charged
  and costed, and `CostConfig::estimates` and `RunMetadata.estimates` list it
  with its `source`, which names the inputs of the estimate.

At most one of `placeholder`, `excluded` and `estimated` is `true`.

`carry` is the benchmark-rate or tom-next part of overnight funding. It is
excluded (the default in the builtin example) or modelled from a dated rate
table (see "Carry" under "Financing").

**Spreads.** `basis` is required on every spread that is not excluded:
`average` for a venue average over a stated period, and `minimum` for a
minimum, "from" or standard figure. A result costed on a minimum is a lower
bound on spread cost, and the run lists those instruments in
`RunMetadata.minimum_spreads`. A `schedule` needs a `tz`, and the other way
round. Its windows must not overlap and must not be empty.

**Costed.** An instrument at a venue is costed when no component is a
placeholder and neither its spread nor its commission is excluded. Every run
records `RunMetadata.costed`, a map from instrument to that flag, beside
`placeholders`, `exclusions` and `estimates`. An estimated component counts as
costed. The sweep report carries `costed` per trial
and for the whole sweep: a sweep is costed when at least one trial completed
and every completed trial is costed. A registry written before task #58 has no
`costed` map and reads back as uncosted.

### Units and raw scales (verified against the cache)

| Instrument | Raw on disk | `raw_scale` | Pip/point |
|---|---|---|---|
| EURUSD | `11036.6` = 1.10366 | `1e-4` | 0.0001 |
| GBPJPY | `17960.1` = 179.601 | `1e-2` | 0.01 |
| XAUUSD | `206362.5` cents = $2063.625 | `1e-2` | $0.01 |
| USA500IDXUSD | `4774.361` points | `1` | 1 point |
| DEUIDXEUR | `17901.597` points | `1` | 1 point |

### Fills

For a fill of `size` units at a bar point and instant `at`:

```
mark     = configured side (bid, ask or mid) x raw_scale
spread   = the schedule window containing `at` (local to spread.tz), else spread.pips
buy      = mark + spread x pip_size / 2 + slippage
sell     = mark - spread x pip_size / 2 - slippage
slippage = slippage.points x pip_size + |mark| x slippage.bps / 10_000
commission (GBP) = per_fill + per_unit_per_fill x size   (+ per_round_trip on the closing fill)
```

The Dukascopy ask − bid is never a cost; the venue-cost Decision on #28
explains why. It is only compared with the instrument's
`divergence_tolerance`. A bar beyond that tolerance fails the fill with
`FillError::Divergence`. Nothing falls back to bid-only or a zero spread.
The instant is the fill's own time (`FillAt::ts`): a close fill on an hourly
bar pays the spread in force at the bar's close, not at its open.

### Volatility slippage and spread excess

Two optional cost models make a fill cost more when the market moves fast or
the spread is wide. Both are off by default: with `model = "fixed"` (or no
`model`) and no `spread_excess` block, every fill, ledger and registry record
is the same as before them.

```toml
# The coefficients are dimensionless; floor_minutes and max_gap_minutes default to 30 and 5.
slippage = { model = "volatility", market = 0.25, stop = 0.5, limit = 0.0, floor_minutes = 30, max_gap_minutes = 5, placeholder = false, estimated = true, source = "..." }
# Optional; baseline_weekdays defaults to 20 (at least 2), cap is at least 1.
spread_excess = { baseline_weekdays = 20, cap = 4.0, placeholder = false, estimated = true, source = "..." }
```

A `volatility` slippage takes no `points` or `bps` (absent or 0), and a
`fixed` one takes none of the five volatility fields. Under `volatility`,
`market`, `stop` and `limit` are required and not negative, and
`floor_minutes` and `max_gap_minutes` are at least 1. Both models read the
instrument's one-minute data, which the loader builds when the request asks
for it (`LoadRequest::with_minute_costs`; a sweep does so for every
instrument whose venue cost uses either model). The load then also reads
`2 x baseline_weekdays` weekdays and the floor's minutes before the window.
That look-back feeds only the cost data: the series, the warm-up and every
other result stay as they are.

The arithmetic, for a fill at instant `τ` (prices on the configured side
unless stated, ranges in quote units through `raw_scale`):

```
R(m)       = ((ask.high + bid.high) - (ask.low + bid.low)) / 2      the range of the mid
             (a one-sided minute: the range of the side it has)
exec(τ)    = market fill (entry, signal or end-of-run exit, liquidation value,
             a close-only stop or target):
               the first minute opening in [τ, τ + max_gap_minutes)
             stop or target inside the engine bar:
               the first minute of the bar whose range reaches the level
               (low <= level below the bar's open, high >= level above it;
               the minute of nearest approach if none does)
             stop or target filled at the bar open (a gap): the bar's first minute
floor(τ)   = median R of the last floor_minutes minutes opening before τ
R_used     = max(R(exec), floor)   (no exec minute: floor; neither: FillError::NoVolatility)
slippage   = κ x R_used,  κ = market | stop | limit by fill kind, never negative

s(m)       = max(ask.open - bid.open, ask.close - bid.close)
baseline   = median s at exec's UTC minute of day over the last baseline_weekdays
             weekdays before τ's UTC date with a two-sided minute there,
             looking back at most 2 x baseline_weekdays weekdays
w          = s(exec) / baseline
extra      = spread(τ) x pip_size / 2 x (min(w, cap) - 1)+

buy        = mark + spread x pip_size / 2 + extra + slippage
sell       = mark - spread x pip_size / 2 - extra - slippage
```

`w` is not computed, and `extra` is 0, when there is no execution minute, the
execution minute is one-sided, fewer than `baseline_weekdays / 2` (rounded up)
baseline values exist, or the baseline is 0 or below. The fill records which
case applied in `spread_ratio_status` (`ok`, `capped`, `no_exec_minute`,
`one_sided`, `baseline_missing`, `baseline_nonpositive`, or `absent` without a
spread excess).

The store's ask − bid is used only as the ratio `w`, which scales the
configured venue spread. It is never the cost itself, so the venue-cost
Decision on #28 still holds
([Decision on #195](https://github.com/radiusred/tradedesk/issues/195#issuecomment-6219124050)).
The divergence check on the engine bar does not change.

**Records.** Under either model every `Fill` carries `fill_kind`
(`market`, `stop` or `limit`), `slippage_model`, `exec_minute`, `range_exec`,
`range_floor`, `range_used`, `kappa`, `slippage`, `spread_ratio`,
`spread_baseline` (quote price; `null` when no baseline exists),
`spread_ratio_status`, `half_spread` (the configured venue half-spread at the
fill instant, quote price), `extra_half_spread` and `spread_excess_cost` (a
field that does not apply is left out). With the one-minute bars, these fields
hold every input of the fill's slippage and spread-excess charge. A fill with a
fixed slippage and no spread excess has none of them. `ClosedTrade.spread_excess_cost` is the sum
over both fills, and the net P&L includes it. The metrics cost breakdown has
`spread_excess` beside `spread` and `slippage`. `RunMetadata.cost_models`
gives each instrument's slippage model and `spread_excess: present | absent`.
Each `Fill` links to its round trip: `Fill.position` equals `ClosedTrade.id`.
The sweep registry writes a trial's fills (`outcome.fills`, every fill in
booking order) when at least one fill has this detail, so that every charge can
be recomputed from the registry. A trial with fixed slippage and no spread
excess has no `fills` key.

**Worked example** (synthetic values; the unit test
`a_round_trip_books_the_detail_and_nets_the_spread_excess`). EURUSD
(`raw_scale` and pip 0.0001), a 1.0-pip venue spread, `market = 0.25`,
`cap = 4.0`. A buy of 10,000 units at the close of the 13:00 bar, `τ` = 14:00,
mark 1.1000:

```
exec       = the 14:00 minute: R = 40 raw = 0.0040, s = 6 raw
floor      = median of the 30 minutes before 14:00 (ranges 1..30 raw) = 15.5 raw = 0.00155
R_used     = max(0.0040, 0.00155) = 0.0040
slippage   = 0.25 x 0.0040 = 0.0010
baseline   = 2 raw (the 14:00 minute on each of the 20 weekdays before)
w          = 6 / 2 = 3, below the cap
extra      = 0.00005 x (3 - 1) = 0.0001
buy        = 1.1000 + 0.00005 + 0.0001 + 0.0010 = 1.10115
costs      = spread 0.50, spread excess 1.00, slippage 10.00 (USD)
```

### Financing

Rollovers happen at the venue's local time, on local weekdays only. The UTC
instant moves with daylight saving through `chrono-tz`.

In the builtin example:

| Venue | Rollover | FX | Metals | Indices | Energy |
|---|---|---|---|---|---|
| `example_spread_bet` | 22:00 `Europe/London` | Wednesday x3 | Friday x3 | Friday x3 | Friday x3 |
| `example_ny_spread_bet` | 17:00 `America/New_York` | Wednesday x3 | Wednesday x3 | Friday x3 | Wednesday x3 |
| `example_ny_cfd` | 17:00 `America/New_York` | Wednesday x3 | Wednesday x3 | Friday x3 | none (optional) |

A position is charged at a rollover when it is held across it, that is
`entry < rollover < exit`. The triple day charges `days_on_triple` and every
other weekday charges 1. That makes seven days for a full week, and each
calendar day is charged once. The charge is
`|size| x mark x rate / day_count x days`. The mark is the configured side's
close of the last 1m bar closed by the rollover instant. Long and short
rates are separate, and a positive rate is a charge.

With the carry excluded, the charge uses the configured annual rate alone.

Each charge is a `FinancingCharge`. It records the position's `direction`
and `size`, the `charge` in quote currency, the `fx_rate` (quote to account,
from the run's `account_fx`, the same rate as the trade's) and
`charge_account = charge x fx_rate` in account currency. A record written
before these fields reads back with `None` for each.

### Carry

A modelled carry is a dated rate table that the ledger charges at each
rollover, beside the financing:

```toml
carry = { placeholder = false, estimated = true, table = "carry/eurusd.csv", sha256 = "<64 hex digits>", day_count = 365, source = "the inputs of the table" }
```

- **The table** is a CSV file with the header
  `date,long_rate,short_rate,long_rate_date,short_rate_date,flag`. `date` is
  the venue-local trading date a rollover closes, ISO format, one row per
  date, ascending. The rates are annual fractions with the cost file's sign
  rule: positive is a charge to the trader, negative a credit.
  `long_rate_date` and `short_rate_date` are the publication dates of the
  inputs behind each rate; neither may be after `date` (a look-ahead).
  `flag` is free text (for example `stale`) and may be empty.
- **Load.** A relative `table` path resolves against the cost file's
  directory (`CostConfig::from_path`, a sweep's `cost_config`, or
  `CostConfig::from_toml_str_at`; `from_toml_str` uses the current
  directory). The file's sha256 must equal `sha256`, or the config does not
  load. `day_count` is the carry's own divisor, 360 or 365.
- **Charge.** At each rollover the financing is charged on (same instant,
  calendar and triple-day rules), the carry charges
  `|size| x mark x rate / day_count x days`, with the long or short rate of
  the row for the rollover's local date. A rollover whose date has no row
  stops the run with `LedgerError::NoCarryRate`: a missing rate is never a
  zero.
- **Records.** Each `FinancingCharge` then has `components`: the
  `financing` and `carry` parts, the `carry_rate` and `carry_day_count` used,
  the whole `carry_row`, and the table's configured `carry_table_path` and
  its `carry_table_sha256`; `charge` is the sum of the two parts.
  `RunMetadata.carry_tables` lists, per instrument, the table's configured
  `path`, its `sha256`, `day_count`, `rows`, `first_date` and `last_date`.
  The carry is listed in `RunMetadata.estimates`, and the instrument stays
  costed. The sweep registry writes every rollover charge
  (`outcome.rollovers`, in booking order) when one has `components`, so that
  every charge can be recomputed. With every carry excluded none of these
  keys is written.

### Dividend

The optional `dividend` component charges a position held over an
ex-dividend date: a long is credited, a short is debited. A venue ×
instrument without it charges nothing, and every record and output is as
before. It has the four provenance states:

```toml
# Estimated: charged from an ex-date table, at the instant tz, time and weekdays_before set.
dividend = { placeholder = false, estimated = true, table = "dividend/x.csv", sha256 = "<64 hex digits>", tz = "America/New_York", time = "16:00", weekdays_before = 1, source = "the inputs of the table" }
# The same with a dated schedule of times in place of `time`, for a close time that changes.
dividend = { placeholder = false, estimated = true, table = "dividend/x.csv", sha256 = "<64 hex digits>", tz = "America/New_York", times = [{ from = "2024-01-01", time = "13:00" }, { from = "2024-01-18", time = "16:00" }], weekdays_before = 1, source = "the inputs of the table" }
# Verified or placeholder: with the same table fields it charges from the table;
# with none of them it is a stated zero and charges nothing.
dividend = { placeholder = false, source = "where the venue states that no adjustment applies" }
# Excluded: charges nothing, takes no table field; results stay costed and list it.
dividend = { placeholder = false, excluded = true, reason = "...", source = "..." }
```

- **The table** is a CSV file with the header
  `date,points,long_share,short_share,bound`, one row per ex-date, ISO
  dates, ascending. `points` is the dividend in index points (`pip_size` in
  quote price, as everywhere in this crate). `long_share` and `short_share`
  are the shares of `points` credited to a long and debited to a short.
  `points` and the shares are finite and not negative. `bound` (`true` or
  `false`) says that `points` is a bound, not a figure for the date. The
  table covers its first row's date to its last row's date; a row with
  `points = 0` states a covered date with no dividend.
- **Load.** As the carry table: a relative `table` path resolves against the
  cost file's directory, and the file's sha256 must equal `sha256`, or the
  config does not load.
- **Instant.** For ex-date `d`, the charge instant is `time` in `tz` on the
  weekday `weekdays_before` weekdays (Monday to Friday) before `d`: 0 is `d`
  itself, 1 the weekday before (at most 5). Every calendar date is a
  candidate, so with `weekdays_before = 1` a Saturday, Sunday and Monday
  ex-date are all charged at Friday's instant. Exchange holidays are not
  modelled: the table's maker states the instant. A position is held over
  the instant as over a rollover: `entry < instant < exit`.
- **Dated times.** An exchange can change its close time inside the
  table's span. In place of `time`, `times` is a list of
  `{ from = "YYYY-MM-DD", time = "HH:MM" }` entries: each entry applies to
  charge instants whose local day (the weekday `weekdays_before` before the
  ex-date, not the ex-date) is on or after its `from` and before the next
  entry's. A charged dividend has `time` or `times`, not both; a single
  `time` is a one-entry schedule and charges as before. The config does not
  load when `times` is empty, when its `from` dates are not strictly
  ascending, or when the first entry is after the charge day of the
  table's first date. Coverage (below) is checked at the instant that
  applies on each date.
- **Charge.** A long is credited `-(points x pip_size x long_share x |size|)`
  and a short is debited `points x pip_size x short_share x |size|`, quote
  currency; `x fx_rate` in account currency. A position held over the
  instant of a date outside the table's first and last date stops the run
  with `LedgerError::DividendOutsideTable`: a date the table does not cover
  is never a zero.
- **Records.** Each charge is one `FinancingCharge` of its own, in
  `Ledger::financing_charges` beside the rollovers and linked to its trade
  by `position`. It is booked at the dividend's instant with `days = 0`, so
  its financing and carry parts are zero, and `charge` is the dividend. It
  has the `direction`, `size`, `fx_rate` and `charge_account` of every
  record, the mark at the instant, and a `dividend` part: the `ex_date`, the
  whole table `row`, the `share` applied, the `charge` and `charge_account`,
  and the table's configured `table_path` and its `table_sha256`. A rollover
  record has no `dividend` part.
- **Totals.** The trade's (and the open position's) `financing` includes
  its dividend charges, so net P&L, the equity curve and the cost breakdown
  pay them. `ClosedTrade.dividend` and `OpenPosition.dividend` keep them
  apart, credits apart from debits: `credit` (0 or negative) and `debit`
  (0 or positive), quote currency. They are absent when the instrument's
  dividend charges nothing.
- **Metadata and registry.** `RunMetadata.dividend_tables` lists, per
  instrument, the table's configured `path`, its `sha256`, `rows`,
  `first_date`, `last_date`, and the instant's `tz`, `time` (or `times`,
  each `{ from, time }` with the time as `HH:MM`) and `weekdays_before`. Only
  the one that is configured is written. An estimated dividend is listed in
  `RunMetadata.estimates`, and the instrument stays costed. The sweep
  registry writes every held-position record (`outcome.rollovers`) when one
  has a `dividend` part. Without a charged dividend none of these keys is
  written.

### The builtin example

Every figure below is **illustrative**: it is not any venue's, and it must not
be used for costed results. Spreads are in pips (FX), dollars × 100 (gold, so
40 = $0.40) or index points. Every `source` in the file says "example".

Two exclusions apply at every example venue and to all five instruments:

- **Slippage is excluded (omitted).** Costed results charge no slippage, so
  execution cost is a lower bound.
- **Carry is excluded (omitted).** Results charge the annual rate to longs and
  shorts alike; the benchmark or tom-next part is not charged. The file has
  no carry table.

| Component | `example_spread_bet` | `example_ny_spread_bet` | `example_ny_cfd` |
|---|---|---|---|
| Spread EURUSD | 1.0 average | 0.8 minimum | 0.3 average |
| Spread GBPJPY | 3.0 average | **excluded (uncosted)** | 1.0 average |
| Spread XAUUSD | 40 ($0.40) minimum | 20 ($0.20) minimum | 25 ($0.25) average |
| Spread USA500IDXUSD | London: 0.5 from 14:30 to 21:00, 2.0 from 21:00 to 23:00, 1.0 otherwise (minimum) | 0.8 minimum | New York: 0.5 from 09:30 to 16:00, 2.0 from 17:00 to 18:00, 1.0 otherwise (minimum) |
| Spread DEUIDXEUR | London: 6 from 22:00 to 01:00, 3 from 01:00 to 08:00, 1.5 from 08:00 to 16:30, 2.5 from 16:30 to 22:00 (minimum) | 1.6 minimum | 1.5 average |
| Commission | 0 | 0 | FX 3.00 per 100,000 units per side; gold 0.03 per ounce per side; indices 0 |
| Financing FX, gold | 2% /360 | excluded (omitted) | excluded (omitted) |
| Financing indices | 3% /360 | 3% /360 | 3% /360 |

Every instrument is **costed** except GBPJPY at `example_ny_spread_bet`. A
result on a minimum spread is a lower bound on spread cost.

A run lists all of this in its metadata: `placeholders` (none),
`exclusions` (with reasons and effects), `minimum_spreads` and `costed`.

### Money

Size is in instrument units. P&L is first in the quote currency, as
`size x price move`, and then in GBP through the run's fixed `AccountFx`
rate. A spread-bet stake in pounds per point maps to units through
`AccountFx::units_for_stake_per_point`. For each `ClosedTrade`,
`gross_pnl_quote` is the move between the two marks, and `net_pnl_quote`
deducts spread, slippage, financing, and commission converted to quote.

### Stops and targets

Levels are raw prices on the configured side.

- `Intrabar`: if the bar opens through a level, the exit fills at the open.
  Otherwise a level the bar's low or high reaches fills at the level. When
  both levels are reached in one bar, `StopFirst` takes the stop at its level
  and `Conservative` takes the stop at the bar's adverse extreme.
- `CloseOnly`: a level counts as hit only when the close reaches it, and the
  exit fills at the close.

The mode and its sub-option are written into `RunMetadata`.

## Metrics

`Metrics::compute(&ledger, &marks, &config)` is a pure function of a finished
run. It clones the ledger and accrues financing to the window's close on the
clone, then rebuilds each day from the ledger's records: fills, closed trades,
open positions and financing charges. The caller's ledger is not changed. All
money is in the account currency (`account_currency`, GBP).

### Day convention

A day is a **UTC weekday, Monday to Friday, marked at its close**, the next
`00:00Z`. That is the instant the UTC daily bar closes. Saturdays and Sundays
are omitted from the curve and from every statistic. Sunday's short FX and
metals session and any weekend exit land in Monday's point. A weekday with no
bars (a holiday) stays in and carries the last mark forward. A position is open
at a close when `entry_ts <= close < exit_ts`.

**A window that ends on a Saturday or Sunday** has no Monday for its weekend
to land in. So the curve ends with one more point after Friday's, with `kind =
"weekend_close"`. It is dated the window's last day and marked at the window's
close (`last_day + 1` at `00:00Z`). It carries every fill, financing charge and
mark move after Friday's close. Its return, from Friday's close to the
window's close, is a daily observation like the others. A month that ends on a
Sunday therefore keeps its Sunday session. Every other point has `kind =
"weekday"`. A window with no weekday at all is refused (`NoTradingDay`).

The day set comes from the window's calendar, not from the data, so every
trial in a sweep has returns on the same dates. The curve's last point is
always the window's close, so for every window `compute` accepts:

```
final equity − starting capital = Σ closed-trade net + open_at_end liquidation net
```

### Mark convention

An open position is valued at **liquidation**: the configured-side close of the
last bar closed by the instant, less the exit half-spread, slippage, exit
commission and per-round-trip commission. It is priced through
`FillModel::quote`, exactly as `Ledger::close` would book it. Financing to date
(the position's charges before the close) and the entry costs are already
deducted. A divergent mark bar is an error. Every point reports the exit cost
it deducted (`open_exit_cost`), so the ledger's mid mark
(`Ledger::mark_to_market`) is `equity + open_exit_cost`.

```
equity        = starting capital + realised net + unrealised net (liquidation)
              = starting capital + gross P&L − costs incurred − open exit cost
simple return = equity / previous equity − 1   (the first against the capital)
log return    = ln(equity / previous equity)
```

Financing is inside both net figures, so it is not subtracted a second time.

### Sharpe

```
excess_t = r_t − rf / 252
Sharpe   = sqrt(252) × mean(excess) / std(excess)    sample std, ddof = 1
```

Here `r_t` is the daily simple return of the curve and `rf` is the annual
risk-free rate (default 0). The Sharpe is always reported next to
`observations`, the number of daily returns, and it is `None` below two
observations or at zero variance. It is never computed per exit day or per
trade. Monday to Friday gives about 261 observations a year, so `sqrt(252)` (the
convention used here) reads about 1.8% lower than `sqrt(261)` would. A window ending
on a weekend adds its one weekend-close observation.

For the sweep's deflated Sharpe, `ReturnStats` also has the per-observation
Sharpe (`sharpe_daily`), the mean, the std, the biased skewness and the Pearson
(non-excess) kurtosis.
**Secondary:** annualised volatility `std × sqrt(252)`, and Sortino
`sqrt(252) × mean(excess) / sqrt(mean(min(excess, 0)²))` with the downside taken
over every observation.

### Drawdown

The drawdown is measured peak to trough on the daily curve, with the starting
capital as the opening point (dated the day before the window). There are two
maxima, because the deepest loss in pounds and in percent can be different
episodes: `by_amount` (`equity − running peak`, first-class when the capital is
capped) and `by_fraction` (`equity / running peak − 1`). Each records
the amount, the fraction, the peak and trough dates and equities, and the
recovery date, which is the first later point at or above the peak (`None` if
there is none). A curve that never falls below a previous peak has no drawdown
(`None`).

### Trade statistics

These are computed over `closed_trades()`, net of every cost:

- count, wins, losses, breakevens and win rate;
- average win and loss, expectancy per trade (net and gross);
- profit factor (`None` when no trade lost);
- average and maximum bars and days held;
- longest winning and losing streaks, in closing order;
- count and net P&L for each exit reason (signal, stop, target, end of run);
- total gross and net P&L, and the cost drag by spread, slippage, commission
  and financing.

Positions still open when the window closes are listed under `open_at_end`
with their liquidation value. They are never counted as closed trades.

### Ruin

If equity reaches zero or below, `ruined_on` records the first such day. Returns
from a non-positive base are `None`, and every return statistic is undefined.
The drawdown fraction is clamped at −100%, while the amount carries the whole
loss.

## Example

```rust
use tradedesk_backtest::{BarTimeframe, LoadRequest, OneSidedPolicy, load};
use tradedesk_data::dukascopy::DukascopyReader;

let reader = DukascopyReader::new("path/to/marketdata");
let first = chrono::NaiveDate::from_ymd_opt(2024, 1, 1).unwrap();
let last = chrono::NaiveDate::from_ymd_opt(2024, 12, 31).unwrap();
let requests = [
    LoadRequest::utc_days("XAUUSD", first, last, [BarTimeframe::M15]),
    LoadRequest::utc_days("USA500IDXUSD", first, last, [BarTimeframe::D1]),
];
let data = load(&reader, &requests, OneSidedPolicy::Reject)?;
let gold_15m = data.series("XAUUSD", BarTimeframe::M15).unwrap();
```

## Indicators and the strategy trait

### Indicators (`tradedesk_backtest::indicators`)

Each indicator implements the `Indicator` trait: `update`, `is_ready`, `reset`,
`warmup_periods` and `batch`. Each matches tradedesk 1.6.2
(`tradedesk/marketdata/indicators`) bit for bit, seeding quirks included. Every
Python `sum()` is reproduced with CPython's compensated float summation (CPython
3.12 and later).

| Indicator | Input | First value | Seeding (Python parity) |
|---|---|---|---|
| `Sma(n)` | close | close `n` | Window re-summed on every update |
| `Ema(n)` | close | close `n` | Seeded with the first close; `alpha = 2/(n+1)` |
| `Atr(n)` | bar | bar `n` | SMA of the first `n` true ranges, then Wilder |
| `Adx(n)` | bar | +DI/−DI at bar `n+1`, ADX at bar `2n` | Wilder sums for TR and ±DM; ADX is the SMA of the first `n` DX values |
| `BollingerBands(n, k)` | close | close `n` | Population std (ddof 0) |
| `Macd(f, s, sig)` | close | bar `s+sig-1` | Each EMA seeded with an SMA, and the seed bar is updated again with the same close |
| `Rsi(n)` | close | close `n+1` | Sums of the first `n` deltas, then Wilder. The value is 100 when there are no losses, else 0 when there are no gains |
| `Vwap(price, session)` | (open time, bar) | first bar with volume | Typical price or close. Resets on the UTC date or at a UTC hour |

Prices are validated at the boundary (RAD-1906). A NaN, infinite or
non-positive price, a bar with `low <= open, close <= high` violated, or a
negative or non-finite volume is an `IndicatorError`, and the indicator's state
is left untouched.

### The strategy trait (`tradedesk_backtest::strategy`)

```rust
pub trait Strategy {
    fn name(&self) -> &'static str;
    fn primary_timeframe(&self) -> BarTimeframe;
    fn context_timeframes(&self) -> &[BarTimeframe] { &[] }
    fn price_scale(&self) -> Option<f64> { None }
    fn on_context_bar(&mut self, tf: BarTimeframe, bar: &JoinedBar) -> Result<(), StrategyError>;
    fn on_bar(&mut self, bar: &JoinedBar) -> Result<Option<Signal>, StrategyError>;
    fn on_feedback(&mut self, feedback: PositionFeedback) -> Result<(), StrategyError>;
    fn position(&self) -> Option<Direction>;
}
```

- **Signals.** A `Signal` is stamped with the open of the primary bar it fired
  on. Its `SignalKind` is one of:
  - `Enter(Entry { direction, reference_price, stop, target, atr })`;
  - `MoveStop { stop }`, used for the breakeven ratchet;
  - `Exit { reason }`;
  - `Reverse { reason, entry }`, a one-fill reversal: close the open position
    for `reason` and open `entry`, in the opposite direction, at the same fill
    (see "One-fill reversal" below).

  Levels are in raw price units and are anchored at `reference_price`, the
  signal bar's close. `SignalExitReason::as_str` gives the Python `exit_reason`
  labels.
- **No fills.** A strategy never fills an order. It keeps the position it has
  signalled, as the Python `PositionTracker` does, and evaluates its own
  close-based exits.
- **Feedback.** The engine corrects the strategy through `PositionFeedback`:
  - `EntryFilled { price }` re-anchors the position at the fill. The price is in
    **raw** units, while the ledger's `Fill::fill_price` is in quote units;
  - `EntryRejected` reverts the strategy to flat and **restarts its cooldown
    exactly as a filled-then-exited position would**, so a refused entry is
    not re-signalled on every bar. A strategy with no cooldown of its own
    should wait a default of its own after a rejection instead;
  - `ClosedExternally` reports a stop or target the ledger took. It flattens the
    position and starts the cooldown.
- **Driving a strategy.** `BarFeed` steps a strategy one primary bar at a time.
  Before each primary bar it delivers every context bar that has closed by then,
  ordered by close time with the longer timeframe first on a tie. `run_signals`
  is the same loop without feedback.
- **Helpers.** `checked_side` validates a bar's side before any state changes,
  `checked_fill` a feedback fill price, and `positive` / `non_negative` a
  config value. `SizingConfig` and `atr_normalised_size` are the sizing inputs
  and arithmetic a strategy may need of its own (the engine sizes entries with
  `SizingPolicy`).

### Bridge to the ledger (`strategy::bridge`)

The strategy and the ledger use different units and types, and the bridge is
the one place they meet.

| | Strategy | Ledger |
|---|---|---|
| Entry price | `Entry::reference_price`: raw units, the signal bar's close on the strategy's side | `Fill::fill_price`: executed price in quote units (raw × `raw_scale`, plus costs) |
| Stop / target | Raw units, anchored at `reference_price` | Raw units on the venue's configured price side |
| Direction | `Direction`: one type, shared with the ledger (`strategy::Direction` re-exports `crate::Direction`) | the same |
| Exit reason | `SignalExitReason` (the Python labels, also its serialised form) | Every strategy exit is booked as `ExitReason::Signal` (`From<SignalExitReason>`) by `Ledger::close_signal`, which keeps the label as `signal_reason` on the closing `Fill` and the `ClosedTrade`. The ledger's `Stop`/`Target` reach the strategy as `ClosedExternally` |

- **Entry.** `FillBridge::new(fill_model)` reads the instrument's `raw_scale`.
  After `Ledger::open`, `FillBridge::anchor(&entry, &fill)` converts the fill to
  raw units (`fill_price / raw_scale`) and shifts the stop and target by
  `raw_fill − reference_price`. The engine sends `anchored.feedback()` to the
  strategy and the shifted levels to `Ledger::set_levels`. Later `MoveStop`
  levels already come from the re-anchored entry and need no shift.
- **One close per bar.** On each bar with a position open, the engine:
  1. calls `Ledger::check_exit`;
  2. always calls `Strategy::on_bar`;
  3. follows `resolve_bar`.

  A ledger trigger wins, and the strategy's `Exit` or `MoveStop` for that bar
  is dropped. If the strategy did not exit itself, it receives
  `ClosedExternally` after its `on_bar`, which leaves it as if it had exited on
  that bar: no same-bar re-entry, cooldown from zero. With no trigger, a
  strategy `Exit` closes at the bar close and a `MoveStop` moves the ledger
  stop.
- **One-fill reversal.** A strategy that holds a position can return
  `Reverse { reason, entry }` to close it and open the opposite one on the same
  bar, with no flat bar between. After that `on_bar` the strategy holds
  `entry.direction`, as after an `Enter`. `resolve_bar` gives
  `BarAction::Reverse { trigger, reason, entry }`:
  - with no ledger trigger, the old position closes at the bar close for
    `reason` (booked as `Signal` with that label);
  - when the ledger's stop or target fired on the bar, the trigger wins for the
    old position (closed at its trigger point, booked as `Stop` or `Target`),
    and the strategy is not sent `ClosedExternally`, because it already left
    that position;
  - either way the new position then opens at the bar close, exactly as an
    `Enter` does, and the strategy receives `EntryFilled` for it.

  A `Reverse` while flat is refused (`BridgeError::OutOfStep`), as an `Exit` or
  a `MoveStop` while flat is: the strategy believes it holds a position the
  ledger does not, and treating the signal as an entry would hide that. A flat
  strategy signals `Enter`. The engine also refuses a `Reverse` whose
  `entry.direction` is the open position's direction. An `Enter` while a
  position is open is still refused, and `Exit` on one bar followed by `Enter`
  on the next books exactly as before, with a flat bar between.
- **Price sides.** The built-in venues price from mid while the strategies read
  bid. The levels keep their distance from the fill either way. The two
  evaluators can see prices half a Dukascopy spread apart, and `resolve_bar`
  still closes each position exactly once.

## The engine (`tradedesk_backtest::engine`)

`engine::run(&mut strategy, &market_data, &cost_config, &EngineConfig)` is one run:
one strategy on one instrument at one venue. It returns the `Ledger`, the
`Metrics` and an `EngineStats` count of what happened. `EngineConfig` holds:
- the instrument and the venue;
- the `ExitEvaluation` and the `AccountFx`;
- a `SizingPolicy`:
  - `StakePerPoint { stake }` uses `AccountFx::units_for_stake_per_point`;
  - `AtrRisk { risk, atr_multiple, min_units, max_units }` sizes so that an
    `atr_multiple` × ATR move costs `risk`. It uses `atr_normalised_size`
    with a point value of `raw_scale` × the quote→account rate;
- an `EndOfRun`: `Close` books a position still open at the last bar's close as
  `end_of_run` with the full exit cost, and `LeaveOpen` leaves it to the
  metrics' liquidation mark;
- the `MetricsConfig` (the window, the starting capital and the risk-free rate).

Each primary bar follows the bridge's order:
1. With a position open, `Ledger::check_exit` evaluates the standing stop and
   target.
2. The strategy sees the bar (`BarFeed::step`, with context bars delivered by
   close time).
3. `resolve_bar` decides what happens:
   - an **entry** fills at the signal bar's close, and `FillBridge::anchor` sends
     the raw-unit fill back to the strategy and the shifted levels to the ledger;
   - a **ledger stop or target** closes at its trigger point, and the strategy
     hears `ClosedExternally` after its `on_bar`;
   - a **strategy exit** closes at the bar close through `close_signal`, which
     keeps its label;
   - a **stop move** goes to `set_levels`;
   - a **reversal** closes the open position (at the bar close through
     `close_signal`, or at the ledger's trigger point if its stop or target
     fired), then opens the opposite position at the bar close as an entry
     does. The ledger records two trades at one fill time: the closing fill of
     the old position and the opening fill of the new one. Each leg is priced
     by the fill model on its own side, so each pays its own spread, slippage
     and commission (the round-trip commission, where the venue has one, on
     the closing leg, as for any close).
4. With a position still open, `accrue_financing` runs at the bar close.
5. The ledger's open position and `Strategy::position` must agree on the
   direction, or both be flat; otherwise the run fails
   (`BridgeError::OutOfStep`).

- **Fill timestamps.** A fill at the close or at a level inside the bar is
  stamped with the bar close. The moment a level traded inside the bar is
  unknown, so the close is used as the conservative choice: any rollover inside
  the bar is charged. A gap fill at the open is stamped with the bar open.
- **Warm-up.** Bars that open before `first_day` reach the strategy, but nothing
  trades on them. An entry signalled there is answered with `EntryRejected`,
  and the strategy restarts its cooldown as if the entry had filled and exited
  on that bar. A cooldown of 0 or 1 bar therefore still allows a signal on the
  next bar, exactly as after an exit. `EngineStats::warmup_entries_rejected`
  counts the rejections.
- **An entry on the final bar** is filled at that bar's close like any other,
  entry cost charged, and is then an ordinary position open after the last
  bar. Under `Close` it is booked at that same close as `end_of_run` with its
  full exit cost (a round trip held for zero bars); under `LeaveOpen` it is
  carried in `open_at_end`. Either way the curve's last point reconciles. The
  rule is written into `RunMetadata.end_of_run` (`{ mode, final_bar_entry:
  "filled_then_end_of_run" }`) in every trial, and
  `EngineStats::final_bar_entries` counts such entries. A reversal on the
  final bar is such an entry too.
- **Reversal counts.** `EngineStats::reversals` counts one-fill reversals. Each
  also counts once in `entries` (the new position) and once in
  `strategy_exits`, or `ledger_exits` when a trigger took the old position, so
  the counts still match the ledger. Records written before the count existed
  read back as 0.
- **Price scale.** A strategy whose signals depend on the price scale (for
  example a notional cap in quote units) declares the scale it assumes
  (`Strategy::price_scale`). It must be the instrument's `raw_scale`, or the run
  fails with `EngineError::PriceScale`.
- **Failures.** A ledger refusal (a divergent bar, no mark or a bad mark), a
  strategy error, a bridge out of step or an unusable size fails the run.
  Nothing falls back, and none of them is a rejected entry.
- **Determinism.** A run uses no clock and no randomness, so the same config and
  data give the same ledger and metrics.

## Sweeps and the trial registry (`tradedesk_backtest::sweep`)

### Registering a strategy

A sweep runs a strategy by name. A `StrategyRegistry` maps each name to a
`StrategyFactory`, written by the crate that owns the strategy:

```rust
pub trait StrategyFactory: Send + Sync {
    /// The config every cell starts from, as JSON: `base` is the spec's `base`.
    /// Never called with `Explicit`; under that base the sweep asks `explicit_schema`.
    fn base_config(&self, base: BaseConfig) -> Result<Value, String>;
    /// Build the strategy from a resolved config, refusing unknown fields.
    fn build(&self, config: Value) -> Result<BoxedStrategy, String>;
    /// Under `base = "explicit"`, the schema of one cell (its leaf paths are the
    /// fields the cell must set). Default: `base_config(BaseConfig::Default)`.
    fn explicit_schema(&self, cell: &BTreeMap<String, Value>) -> Result<Value, String> { .. }
    /// The config path the sweep writes each instrument's `raw_scale` into, if any.
    fn raw_scale_path(&self) -> Option<&str> { None }
}

let mut strategies = StrategyRegistry::new();
strategies.register("my_strategy", MyFactory)?; // a name registers once
```

The name is recorded in every registry record and hashed into every trial id,
so it should not change once trials are recorded under it. `run_sweep`,
`validate_spec` and the command line's `sweep` take the registry; a spec that
names a strategy it does not hold is refused with the registered names.

### The spec

A `SweepSpec` is TOML (`SweepSpec::from_toml_str` / `from_path`). Unknown keys
are errors.

```toml
name = "a label"                           # optional; not part of any id
strategy = "my_strategy"                   # a name in the StrategyRegistry
base = "default"                           # | frozen (the default), if the factory provides one
                                           # | explicit: each cell sets every field
instruments = ["XAUUSD"]                   # each is crossed with every parameter cell
first_day = "2024-01-01"                   # inclusive UTC days
last_day = "2024-01-31"
warmup_days = 90                           # calendar days loaded before first_day (default 0)
venue = "my_venue"                         # a venue id in the cost config
# cost_config = "path/to/costs.toml"       # absent: the builtin example config/costs.toml;
                                           # sweep --cost-config replaces it
exit_evaluation = { mode = "intrabar", both_hit = "stop_first" }
                                           # | both_hit = "conservative" | { mode = "close_only" }
account_fx = { account_currency = "GBP", rates = { USD = 0.79 } }
sizing = { kind = "stake_per_point", stake = 0.1 }
# | { kind = "atr_risk", risk = 100.0, atr_multiple = 2.0, min_units = 1.0, max_units = 1000.0 }
end_of_run = "close"                       # | leave_open
starting_capital = 25000.0                 # account currency
risk_free_rate = 0.0                       # annual (default 0)

[grid]                                     # or [[cells]], an explicit list
period = [10, 20, 40]
take_profit = [8.0, 12.0]
"filter.confirm_on" = [2]                  # dotted paths reach nested fields

[provenance]                               # optional; any keys, string values
authorised_by = "docs/plan.md"
"document sha256" = "9f86d081…"
```

A cell maps parameter paths to values. The grid expands as a cartesian product
with its keys in sorted order and the last key varying fastest. To resolve a
cell, the base config is serialised to JSON, each path is set, and the result
is deserialised with unknown fields refused before the strategy validates it.
When the factory names a `raw_scale_path` (for example `sizing.raw_scale`),
every cell's value there is then set to its instrument's `raw_scale` from the
cost config, so a signal that depends on the price scale is in quote units and
the value is in the trial's recorded `config`. The spec is validated before
anything runs (`validate_spec` runs the same checks on their own):
- grid and cells are not both given, and no grid list is empty;
- every instrument is priced at the venue and has an FX rate;
- the strategy is registered, and its factory has the spec's `base`;
- every parameter path exists in the base config, and none is the factory's
  `raw_scale_path`, which is derived;
- under `base = "explicit"`, every cell sets every field (below).

A value the strategy rejects becomes that cell's recorded failure.

#### Provenance

The optional `[provenance]` table binds a registry to whatever authorised the
sweep, for example a document and its hash. It maps string keys to string
values.
- Any key is accepted: the unknown-key check does not reach inside the table.
  A value that is not a string (a number, a boolean, an array or a nested
  table) is a parse error.
- The engine gives no key a meaning and checks nothing inside the table.
- It is echoed verbatim: in the `sweep` record as `spec.provenance`, and in
  every `trial` record as `provenance`, so a trial line carries it without its
  header.
- It is in the `sweep_id`, because that hashes the whole spec. It is in no
  `trial_id`, because it is not an input to a result: a trial keeps its id when
  only the provenance changes.
- Absent or empty, it is not written at all, so every record and id is the same
  as for a spec without the table.

#### Explicit base

Under `base = "explicit"` there is no base: each cell alone is the strategy's
config. The sweep takes each cell's set of fields from the factory's schema
for that cell (`explicit_schema`), by default the strategy's default config,
serialised to JSON. It uses its **leaf paths** (the dotted paths to values
that are not non-empty objects) and none of its values.
- A cell must set every leaf path, except the factory's `raw_scale_path`,
  which the sweep sets as for the other bases. A cell that omits one is
  refused, with every missing path and the cell's index (counted from 0).
- A path that is not a leaf path is refused, as for the other bases. A path
  that names an object (for example `filter` when `filter.confirm_on` is a
  field) is refused too: set its fields one by one.
- The config is built from the cell's values alone, so the trial record's
  `config` holds the cell's value for every field.
- A strategy with no default config cannot use explicit base, and the spec is
  refused with that reason.

**A config with more than one form.** Some strategies take one of several
config forms, each with fields the others refuse (a field of the config names
the form). The default schema is one form only, so under it every cell of
another form is refused. Such a factory overrides `explicit_schema` to return
the schema of the form the cell names. The sweep asks once per cell and checks
each cell against its own form's leaf paths, so one sweep can hold cells of
every form. A cell that mixes two forms sets a path its form does not have and
is refused, as is a cell that omits a field of its form. When the cell names
no form the factory returns an error, and the spec is refused with it and the
cell's index. The default changes nothing for a factory that does not
override it: every cell, record and trial id stays the same.

These checks are part of the spec's validation, so `validate_spec` makes them
and a sweep makes them before it loads data or runs a trial.

**A field with a serde default** (`#[serde(default)]`) is required like any
other. It is in the serialised default config, so a cell that omits it is
refused; the serde default never fills it. A field that the serialised config
leaves out (for example one under `skip_serializing_if`) is not in the set:
no base lets a cell set it, and only the strategy's deserialiser decides its
value. So a factory's default config should serialise every field, `None`
included (as `null`).

**A grid** under explicit base expands as usual, and every key is in every
cell. So the grid must name every field; a field held fixed is a one-value
list:

```toml
base = "explicit"

[grid]
period = [10, 20, 40]
take_profit = [8.0, 12.0]
"filter.confirm_on" = [2]                  # fixed, but still named
```

### Running

`run_sweep(&spec, &strategies, &reader, &mut sink)` loads every instrument
**once**, from `first_day − warmup_days` to `last_day`, at every timeframe the
cells' strategies need. `run_sweep_on(&spec, &strategies, &market_data, &mut sink)`
takes data already loaded.
- The cells run under rayon, and every one of them borrows the same
  `MarketData`.
- As each cell finishes, its record goes to the sink through a reorder buffer,
  so records stream in cell order and two runs write the same registry apart
  from timestamps.
- A cell that fails or panics is recorded as a failure, and the rest of the
  sweep carries on.
- There is no async code. CPU work runs on rayon only.
- `run_sweep_cancellable` takes a cancel flag (the CLI's SIGINT). Once it is
  set no new cell starts, cells already running finish, records already in
  cell order are written whole, and the sweep returns `SweepError::Cancelled`.

### The registry (JSON Lines, append-only)

`JsonlRegistry::append(path)` writes one record per line and flushes each one.
Each record is serialised whole, newline included, and written to the file
(opened in append mode, with no buffer of its own) with one `write_all`. On a
local POSIX filesystem that is one append another writer cannot split, so
several sweeps or processes can append to one registry; network filesystems
(NFS) do not promise this. `read_registry(path)` reads the file back, and the
result is lossless: the records compare equal to the ones written. Every number is read by
`sweep::json::parse_exact`, which is correctly rounded. serde_json's default
parser can read a 17-digit float back one ULP off, and its `float_roundtrip`
feature would unify into the workspace and move pinned goldens. The
records are:

| `kind` | Fields |
|---|---|
| `sweep` | `schema_version` (1), `sweep_id`, `code_version`, `code_revision`, `spec` (as given, `provenance` included), `cost_config` `{source, blake3}`, `run` (`RunMetadata`), `cells`, `started_utc` |
| `trial` | `schema_version`, `trial_id`, `sweep_id`, `index`, `code_version`, `code_revision`, `strategy`, `instrument`, `params` (the cell), `config` (the resolved strategy config, `null` if it did not resolve), `first_day`, `last_day`, `warmup_days`, `venue`, `cost_config`, `exit_evaluation`, `account_fx`, `sizing`, `end_of_run`, `starting_capital`, `risk_free_rate`, `run` (`RunMetadata`: placeholders, exclusions and the derived `costed` map), `provenance` (the spec's table; absent when it is empty), `started_utc`, `finished_utc`, `outcome` |

`outcome` is one of:
- `{status: "ok", engine: EngineStats, metrics: Metrics, trades, fills, rollovers}`, with the
  full metrics, the equity curve and every closed trade (`ClosedTrade`, in closing
  order; the walk-forward attributes them to its test windows). A ruined run is `ok`,
  and its metrics say it was ruined. A record written before task #62 has no `trades`
  and reads back with `None`. `fills` is every ledger `Fill` in booking order, written
  only when at least one fill has the volatility slippage or spread excess detail
  (task #199); otherwise it is absent and reads back with `None`. `rollovers` is
  every `FinancingCharge` in booking order, written only when a charge has
  `components` (a modelled carry, task #203) or a `dividend` part (task #209),
  each with its side, size, FX rate and account-currency charge (task #205);
  otherwise it is absent and reads back with `None`. The report and the walk-forward read neither;
- `{status: "failed", stage: "config" | "run" | "panic", error}`.

The ids are blake3 hashes:
- **`trial_id`** hashes the canonical JSON of everything the result depends on:
  the code version (`CARGO_PKG_VERSION`) and build (`code_revision`), the
  strategy, the params and resolved config, the instrument, the window and
  warm-up, the venue, the blake3 of the cost config's TOML text (which pins
  each carry and dividend table by its sha256), the exit mode,
  the FX, the sizing, the end-of-run rule, the capital and the risk-free rate.
- **`code_revision`** is `tradedesk_data::CODE_REVISION`: the git commit the
  workspace was built from, prefixed `dirty-` for a modified tree, or `unknown`
  without git. Two builds of one release therefore never share an id, and a
  sweep re-run on newer code is not deduplicated against stale results. Ids
  written before the build was hashed (M1) differ from today's for the same
  inputs, and those records read back with `code_revision` absent.
- The sweep's other cells, its `name` and its `provenance` are not in the
  trial id, so a trial keeps its id when the grid grows.
- **`sweep_id`** hashes the code version and build, the whole spec (`name` and
  `provenance` included) and the cost hash.

Reading is strict:
- **`schema_version`.** Every line's `schema_version` must be one this reader
  knows (1); anything else, or none, is refused with the line number
  (`RegistryError::UnsupportedSchema`). Additive fields keep the version, with
  serde defaults, so older lines still read.
- **A truncated tail.** Bytes after the last newline mean a write was cut
  short, for example by a crash. `read_registry` and `JsonlRegistry::append`
  both refuse the file with `RegistryError::TruncatedTail { offset }`, where
  `offset` is the byte at which the incomplete line starts. Nothing is repaired
  or skipped automatically. Truncating the file to `offset` bytes (for example
  with coreutils `truncate -s`) keeps every complete record.

### Deflated Sharpe and PBO (`SweepReport`)

`SweepReport::from_records(&records, sweep_id, ReportOptions)` reports on one
sweep. A trial id seen twice, from a sweep re-run into the same file, counts
once.

**Costed.** Each `per_trial` entry carries `costed`, taken from its trial's
`run` metadata for its instrument. The report's own `costed` is true only when
at least one trial completed and every completed trial is costed. A result
from a sweep that is not costed must not be reported as costed (see "Costs and
financing").

**Deflated Sharpe** (Bailey & López de Prado, 2014). Every quantity is per
daily observation:

```
PSR(SR*) = Φ( (SR̂ − SR*) · √(T − 1) / √(1 − γ₃·SR̂ + (γ₄ − 1)/4 · SR̂²) )
SR₀      = σ_SR · [ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]
DSR      = PSR(SR₀)
```

- `SR̂`, `T`, `γ₃` and `γ₄` are the trial's `sharpe_daily`, `observations`,
  `skewness` and `kurtosis` (Pearson). γ is the Euler–Mascheroni constant.
- `N` counts the completed trials, including ruined and no-trade trials, plus
  `ReportOptions::prior_trials` (default 0) for a cumulative count. Failed
  cells are not trials.
- `σ_SR` is the sample std (ddof 1) of the defined daily Sharpes.

The assumptions:
- trials are treated as independent, so `N` is not reduced for correlated
  trials;
- the variance term is floored at 1e-12;
- `SR₀ = 0` when `N ≤ 1` or `σ_SR` is undefined or zero.

The report gives every trial's DSR. For the best trial (the highest daily
Sharpe), it gives:
- the raw annualised Sharpe (`√252·SR̂`);
- the annualised benchmark (`√252·SR₀`);
- the deflated excess (`√252·(SR̂ − SR₀)`);
- the DSR;
- the probability it is a false discovery (`1 − DSR`).

**PBO by CSCV** (Bailey, Borwein, López de Prado & Zhu, 2017).
1. Build the matrix: one column per completed, non-ruined trial, holding its
   `Metrics::daily_returns()`. All the columns are on the same dates (the
   window's weekdays, plus its weekend close if it ends on a weekend), and the
   report checks this.
2. Split the rows into `S` contiguous blocks (`np.array_split` sizes; `S`
   defaults to 16).
3. For each of the `C(S, S/2)` choices of in-sample blocks, the rest are
   out-of-sample. Take the trial with the best in-sample Sharpe (mean / sample
   std) and find its out-of-sample rank (ties averaged, undefined Sharpes
   lowest). Then `ω = rank / (N + 1)` and `λ = ln(ω / (1 − ω))`.
4. **PBO is the share of `λ ≤ 0`.** The report gives it with the logit
   distribution: count, mean, std, min, quartiles, median and max.

The purge (`label_horizon`) and the embargo default to 0, which is the paper's
plain CSCV. Daily returns do not overlap, so there is nothing to purge; a purge
(for example `label_horizon = 1`) is available through `CscvConfig`.

### Walk-forward (`WalkForwardReport`)

`WalkForwardReport::from_records(&records, sweep_id, WalkForwardOptions)` runs a rolling
walk-forward over one sweep's registry records. Every trial is one continuous run over
the whole window, and the walk-forward only chooses whose days to read; nothing is
re-run.

- **Folds.** From the window's first day: a train window of `train_months` (24), then a
  test window of `test_months` (6), stepped by `step_months` (6, at least the test
  window). Folds continue while a test window starts on or before the window's last
  day, and the last test window is clipped at that day. Over six years and two months
  that gives nine folds, the ninth tested on the last two months. A fold's days are the equity points dated inside its windows ("Day convention").
- **Selection.** In each fold, the completed trial that is not ruined with the highest
  annualised Sharpe of its daily simple returns on the train days. A tie goes to the
  lower cell index; an undefined Sharpe (fewer than two days, or no variance) ranks
  last. A trial ruined anywhere in the window is never selected, as in the PBO matrix.
- **Stitched out-of-sample series.** The selected trial's daily returns on its fold's
  test days, fold after fold, and their Sharpe (√252, "Sharpe" above). The £ curve
  opens at the starting capital the day before the first test day and adds the
  selected trial's daily £ equity change on each test day; its maximum drawdown is
  measured as in "Drawdown".
- **Out-of-sample trades.** A selected trial's closed trades that its equity curve books
  inside the test window: `exit_ts` after the mark of the last point before the window,
  and at or before the mark of the window's last point. A trade opened in the train
  window and closed in the test window counts, with all of its costs. They are
  summarised as `TradeStats` (count, gross, net and cost by component). The **cost
  drag** is their total cost over their gross P&L, given only when that gross is above
  zero. It needs the registry's `trades`, so a record without them leaves it `None`.
- **Also reported:** each fold's windows, day counts and selected cell with its train
  (IS) and test (OOS) Sharpe, £ P&L and trade count; the mean IS and OOS Sharpe over
  the folds; and every completed cell's own fixed-parameter Sharpe, gross and net £
  P&L and trade count over the whole out-of-sample span.

A position open across a fold boundary carries its P&L into the test window, because
each trial is one continuous run. Folds of a sweep whose trials' dates differ are
refused (`ReportError::MisalignedDates`), as in the report.

## Command line (`tradedesk_backtest::cli`)

The command line (default `cli` feature) runs a sweep spec and reports on a
registry. `cli::main(&strategies)` is the whole process (the stderr log
subscriber, the SIGINT handler, the arguments, stdout), and `cli::run` the same
over given arguments and a given stdout. `sweep` runs the strategies in the
registry it is given, so a strategy crate builds its own binary:

```rust
fn main() -> std::process::ExitCode {
    let mut strategies = StrategyRegistry::new();
    strategies.register("my_strategy", MyFactory).unwrap();
    tradedesk_backtest::cli::main(&strategies)
}
```

The `tradedesk-backtest` binary in this crate registers no strategy: `report` and
`walkforward` work on any registry, and `sweep` refuses every spec with the
registered names (none). With a binary that registers strategies:

```sh
my-backtest sweep spec.toml --out registry.jsonl --cache-root path/to/marketdata \
    --cost-config path/to/costs.toml
tradedesk-backtest report registry.jsonl
tradedesk-backtest report registry.jsonl --format json
tradedesk-backtest walkforward registry.jsonl
```

`walkforward` needs a window longer than its train window (24 months by default);
a shorter one finds no fold and exits 1.

`--cache-root` may come from `TRADEDESK_CACHE_ROOT` instead.

`--cost-config` names the cost config file (the schema in "Config schema"). It
replaces the spec's `cost_config`, with a warning on stderr when the two
differ, and is set in the spec before validation. So the sweep header records
the file that was used: `spec.cost_config` and `cost_config` `{source, blake3}`
both name it, and its blake3 is part of the sweep and trial ids. Without
`--cost-config` the spec's `cost_config` is used, or the builtin example when
the spec has none. A relative path is relative to the working directory.

| Command | What it does |
|---|---|
| `sweep <SPEC> --out <REGISTRY> --cache-root <DIR> [--cost-config <COSTS>]` | Validates the spec (the format in "The spec") and the cost config, opens the registry for appending (created if absent), loads the data once and runs every cell |
| `report <REGISTRY> [--sweep <ID>] [--format text\|json] [--cscv-blocks <S>] [--prior-trials <N>]` | Reports one sweep's deflated Sharpe and PBO. `--sweep` is needed when the registry holds several sweeps. `--cscv-blocks` defaults to 16 and `--prior-trials` to 0 |
| `walkforward <REGISTRY> [--sweep <ID>] [--format text\|json] [--train-months <M>] [--test-months <M>] [--step-months <M>]` | Reports one sweep's rolling walk-forward ("Walk-forward" above). The months default to 24, 6 and 6 |

**stdout carries results only, as whole lines.**
- `sweep` writes one JSON line when the data is loaded (`"kind": "sweep"`,
  with the sweep id, the cell count, the code version and build, and the
  registry path).
- Then it writes one line per trial as it is recorded (`"kind": "trial"`: the
  index, trial id, instrument, params and status, then the Sharpe, total
  return, trade count and entries, or the failure stage and error).
- It ends with a summary line (`"kind": "summary"`: the cell, ok and failed
  counts).
- `report` writes the report as text, or with `--format json` as one JSON
  object (`SweepReport`). `walkforward` does the same with a `WalkForwardReport`.
- Every line is built whole before it is written. JSON lines go through
  one write of a whole line, and the text report is a single write. A failure
  never leaves a partial line.

**stderr carries diagnostics** through `tracing`. `RUST_LOG` filters them
(the default is `info`), and colour is used only on a terminal.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | Done. For `sweep`, every cell completed |
| 1 | Input refused before anything ran: the spec is unreadable or invalid, the cost config is missing or invalid, or the registry is unreadable, truncated (`TruncatedTail`), of an unknown schema, or has no single sweep to report. For `walkforward`, also invalid months or a window too short for one fold. A refused spec creates no registry file |
| 2 | Usage error (clap) |
| 3 | The sweep finished, and at least one cell is recorded as a failure |
| 4 | A runtime failure: the market data could not be loaded, or the registry or stdout could not be written |
| 130 | Interrupted by SIGINT. It wins over every other code |

**SIGINT.** The handler is installed before the arguments are parsed, and Ctrl-C
stops the sweep cooperatively:
- no new cell starts;
- cells already running finish;
- records already in cell order are written whole;
- the process exits 130.

The registry then holds whole lines only, and `report` works on it. Loading the
market data is not interrupted: the flag is checked once the load returns.

## Tests

`cargo test -p tradedesk-backtest` runs unit tests, proptests and golden tests
over the join and aggregation, the loader, fills (the volatility slippage and
the spread excess included), financing, stops and targets,
the ledger, the metrics, the indicators, the bridge, the engine, the sweep, the
registry, the deflated Sharpe, the PBO, the walk-forward and the command line.
None of them needs market data on disk: synthetic caches are written under
`CARGO_TARGET_TMPDIR`.

- **The test strategy.** The engine, bridge, sweep and command-line tests drive
  `ThresholdCross` (`tests/toy/mod.rs`): it goes long when the close crosses up
  through a fixed level, with a stop and a target at fixed distances, and exits
  on a close below a second level. It exists only for the tests and makes no
  claim to be a trading idea. The crate's unit tests include the same file under
  `cfg(test)`.
- **Goldens.** `tests/fixtures/indicators/` pins every indicator against
  tradedesk's Python indicators, and `tests/fixtures/metrics_golden.json` pins the
  Sharpe, volatility, Sortino, skewness, kurtosis and both maximum drawdowns
  against pandas/numpy; the repository's Python suite recomputes both from their
  inputs on every run (`tests/test_rust_crate_goldens.py`).
  `tests/fixtures/overfit_golden.json` is a regression golden for the deflated
  Sharpe and the PBO: fixed returns matrices, and expected values computed by this
  crate's own `sweep::dsr` and `sweep::cscv`. The ignored test
  `overfit_goldens::regenerate_the_golden` rewrites them, and the file's
  `provenance` names it.
- **Command line.** The shipped binary registers no strategy, so the sweep paths
  run in-process through `cli::run` with the test strategy registered; `report`,
  `walkforward`, usage errors, SIGINT and the empty-registry refusal run the
  built binary.
