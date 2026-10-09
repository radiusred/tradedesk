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
| `LoadRequest` | A symbol, a half-open UTC window and the timeframes to build. `utc_days` covers whole inclusive days. |
| `OneSidedPolicy` | `Reject` (the default) fails the load on any one-sided minute. `Report` keeps the two-sided bars and attaches the list. |
| `load` → `MarketData` / `InstrumentSeries` | Reads each `(symbol, side)` once, in parallel across instruments, and returns read-only `Send + Sync` data. |
| `CostConfig` | The validated cost configuration. `builtin()` loads the checked-in `config/costs.toml`, and `from_path` / `from_toml_str` load an override. Per venue, `placeholders` lists every unverified component, `exclusions` every excluded one, `minimum_spreads` the instruments whose spread is a published minimum, and `costed` which instruments can be reported as costed. |
| `InstrumentSpec` | Venue-independent units: `asset_class`, `quote_currency`, `raw_scale` (raw → quote), `pip_size`, and `divergence_tolerance`. |
| `Venue` / `VenueInstrumentCost` | A venue's rollover clock, weekend rules and per-instrument `price_side`, `Spread`, `Slippage`, `Commission`, `Financing` and `Carry`. Each component (`CostComponent`) has its own `source` and a `ProvenanceStatus`: verified, placeholder or excluded (`provenance()`, `is_costed()`). |
| `Spread` / `SpreadBasis` / `SpreadWindow` | The venue spread: a flat `pips`, or a time-of-day `schedule` of local windows in `tz` (`pips_at(instant)`), with a `basis` of `average` or `minimum`. |
| `Exclusion` / `ExclusionEffect` | An excluded component in a run's metadata, with its reason and whether results stay costed with the cost `omitted` or are `uncosted`. |
| `FillModel` | One venue × instrument. `quote(bar, PricePoint, at, TradeSide, size)` prices a fill at instant `at` as a `FillQuote`. `reference(bar)` gives the configured side's OHLC, and `mark_close(bar)` gives the mark. |
| `PricePoint` | `Open`, `Close` or `Level(raw)`: where in the bar a fill happens. |
| `FillError` | `Divergence` (the bar's ask − bid is beyond tolerance), `InvalidSize` or `NonFinite`. A fill error is never resolved by a fallback. |
| `RolloverCalendar` / `Rollover` | A venue's local rollover instants between two UTC times, each with the days it charges. |
| `ExitEvaluation` / `BothHit` | `Intrabar { both_hit: StopFirst \| Conservative }` or `CloseOnly`. `evaluate` returns an `ExitTrigger` with an `ExitReason` and a `PricePoint`. |
| `Ledger` | The book for one run at one venue: `open`, `check_exit`, `set_levels`, `close`, `close_signal` (a strategy exit, booked as `Signal` with its `SignalExitReason` label), `accrue_financing` and `mark_to_market`, plus read access to `fills`, `financing_charges`, `open_positions` and `closed_trades`. |
| `Fill` / `ClosedTrade` / `OpenPosition` / `FinancingCharge` / `PositionMark` | The ledger's records. All of them are `Serialize`. |
| `RunMetadata` | The venue, product, `ExitEvaluation` and `AccountFx` of a run, and its venue's cost provenance: `placeholders`, `exclusions`, `minimum_spreads` and the derived per-instrument `costed` map (`is_costed(instrument)`), recorded with its results, and `end_of_run` (an `EndOfRunRule`: the `EndOfRun` mode and the `FinalBarEntry` rule) when the engine drove the run. |
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

```toml
[instruments.EURUSD]
asset_class = "fx"              # fx | metal | index: selects the weekend rule
quote_currency = "USD"
raw_scale = 0.0001              # quote price = raw cache price x raw_scale
pip_size = 0.0001               # one pip/point in quote price
divergence_tolerance = 100.0    # max bar |ask - bid| in pips/points a fill accepts

[venues.ig_spread_bet]
name = "IG spread bet"
product = "spread_bet"          # spread_bet | cfd
rollover = { tz = "Europe/London", time = "22:00" }
weekend.fx = { triple_day = "wednesday", days_on_triple = 3 }
weekend.metal = { triple_day = "friday" }
weekend.index = { triple_day = "friday" }

[venues.ig_spread_bet.instruments.EURUSD]
price_side = "mid"              # bid | ask | mid
# Every component carries its own provenance; all five are required.
spread = { pips = 1.04, basis = "average", placeholder = false, source = "..." }  # full width
slippage = { placeholder = false, excluded = true, reason = "...", source = "..." }
commission = { per_fill = 0.0, per_unit_per_fill = 0.0, per_round_trip = 0.0, placeholder = false, source = "..." }  # GBP
financing = { long_rate = 0.015, short_rate = 0.015, day_count = 360, placeholder = false, source = "..." }
carry = { placeholder = false, excluded = true, reason = "...", source = "..." }

[venues.ig_spread_bet.instruments.USA500IDXUSD.spread]   # a spread by time of day
pips = 0.6                      # outside every window
basis = "minimum"
tz = "Europe/London"            # the windows' local time zone
schedule = [
    { from = "14:30", to = "21:00", pips = 0.4 },   # [from, to); to < from wraps midnight
    { from = "22:00", to = "23:00", pips = 1.5 },
]
placeholder = false
source = "..."
```

Unknown fields are load errors. Every load is validated: costs must be finite
and non-negative, the zone and time must parse, every asset class needs a
weekend rule, `day_count` must be 360 or 365, every venue instrument must be
defined under `[instruments]`, and every component must have a non-empty
`source`. The amounts inside `slippage` and `commission` default to zero; the
blocks themselves and their provenance do not, so a zero is always stated
with a source.

**Provenance.** Each component is in exactly one of three states:

- **verified** (`placeholder = false`): a figure the venue publishes. The
  `source` names the page, its URL and the date it was read.
- **placeholder** (`placeholder = true`): an assumption or a stand-in. A run
  that uses one is never costed. The builtin config has none.
- **excluded** (`excluded = true` with a `reason`): the venue publishes no
  figure. The component charges nothing, so every amount in it must be zero,
  and an excluded spread has no `basis` or schedule. What that does to a
  result depends on the component (`CostComponent::exclusion_effect`):
  - An excluded **spread** or **commission** makes the venue × instrument
    **uncosted**. It still runs, at no spread or commission, so it can be used
    for signal work, but it is never reported as costed.
  - Excluded **slippage**, **financing** or **carry** is **omitted**. The
    result stays costed and lists what it leaves out.

`carry` is the benchmark-rate or tom-next part of overnight funding. It is not
modelled, because the config holds no rate history, so it must be excluded.

**Spreads.** `basis` is required on every spread that is not excluded:
`average` for a venue average over a stated period, and `minimum` for a
minimum, "from" or standard figure. A result costed on a minimum is a lower
bound on spread cost, and the run lists those instruments in
`RunMetadata.minimum_spreads`. A `schedule` needs a `tz`, and the other way
round. Its windows must not overlap and must not be empty.

**Costed.** An instrument at a venue is costed when no component is a
placeholder and neither its spread nor its commission is excluded. Every run
records `RunMetadata.costed`, a map from instrument to that flag, beside
`placeholders` and `exclusions`. The sweep report carries `costed` per trial
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

### Financing

Rollovers happen at the venue's local time, on local weekdays only. The UTC
instant moves with daylight saving through `chrono-tz`.

| Venue | Rollover | FX | Metals | Indices |
|---|---|---|---|---|
| IG spread bet | 22:00 `Europe/London` | Wednesday x3 | Friday x3 | Friday x3 |
| Pepperstone (spread bet, Razor) | 17:00 `America/New_York` | Wednesday x3 | Wednesday x3 | Friday x3 |

A position is charged at a rollover when it is held across it, that is
`entry < rollover < exit`. The triple day charges `days_on_triple` and every
other weekday charges 1. That makes seven days for a full week, and each
calendar day is charged once. The charge is
`|size| x mark x rate / day_count x days`. The mark is the configured side's
close of the last 1m bar closed by the rollover instant. Long and short
rates are separate, and a positive rate is a charge.

The charge uses the venue's annual admin fee alone, because the benchmark or
tom-next carry is excluded (see below).

### Figures and provenance

Every figure below is the venue's own, read on **2026-10-07**. The full
citation for each component is its `source` string in `costs.toml`, and the
Decisions on #58 record the choices. Spreads are in pips (FX), dollars × 100
(gold, so 30 = $0.30) or index points.

Two exclusions apply at every venue and to all five instruments:

- **Slippage is excluded (omitted).** Neither venue publishes a slippage
  figure. Costed results charge no slippage, so execution cost is a lower
  bound.
- **Carry is excluded (omitted).** No venue publishes a rate history.
  Costed results charge the admin fee to longs and shorts alike. Compared with
  the venue's real funding, a long is under-charged by the benchmark rate, a
  short is over-charged by it (a venue normally credits an index short the
  benchmark minus the fee), and the FX or gold interest differential is
  missing. That differential can be a credit or a debit on either side.

**IG spread bet**

| Component | Figure | Source | Date | Status |
|---|---|---|---|---|
| Spread EURUSD | 1.04 average (minimum 0.6) | [Forex spread betting product details](https://www.ig.com/uk/help-and-support/articles/681881-forex-spread-betting-product-details), Mon 00:00–Fri 22:00 GMT average | 12 weeks to 2021-01-08 | verified, average |
| Spread GBPJPY | 3.86 average (minimum 2.5) | same page | 12 weeks to 2021-01-08 | verified, average |
| Spread XAUUSD | 30 ($0.30) | [Commodities spread bet product details](https://www.ig.com/uk/help-and-support/articles/681833-commodities-spread-bet-product-details), "standard spread" | current | verified, minimum |
| Spread USA500IDXUSD | London: 0.4 from 14:30 to 21:00, 1.5 from 22:00 to 23:00, 0.6 otherwise | [Spread bet Indices product details](https://www.ig.com/uk/help-and-support/articles/682128-spread-bet-indices-product-details) | current | verified, minimum, schedule |
| Spread DEUIDXEUR | London: 4 from 00:15 to 07:00, 2 from 07:00 to 08:00, 1.4 from 08:00 to 16:30, 2 from 16:30 to 21:00, 5 from 21:00 to 00:15 | same page; peak 1.4 from [IG charges](https://www.ig.com/uk/charges) | current | verified, minimum, schedule |
| Commission, all five | 0 | [Fees and costs of spread betting and CFD trading](https://www.ig.com/uk/help-and-support/articles/681705-what-are-the-fees-and-costs-of-spread-betting-and-cfd-trading): commission only on CFD share trading | current | verified |
| Financing FX, gold | 1.5% /360, long and short | [Overnight funding](https://www.ig.com/uk/help-and-support/articles/681712-what-is-overnight-funding-how-is-it-charged-and-how-is-it-calculated) | current | verified |
| Financing indices | 3.4% /360, long and short | same page | current | verified |

All five IG instruments are **costed**. Gold and both indices are costed on
minimums, so their spread cost is a lower bound.

**Pepperstone spread bet**

| Component | Figure | Source | Date | Status |
|---|---|---|---|---|
| Spread EURUSD | 0.5 | [Trading costs and fees](https://pepperstone.com/en-gb/trading/costs-and-fees), "Spread bet min spread" | current | verified, minimum |
| Spread XAUUSD | 10 ($0.10) | same page | current | verified, minimum |
| Spread USA500IDXUSD | 0.4 | same page | current | verified, minimum |
| Spread DEUIDXEUR | 0.9 | same page | current | verified, minimum |
| Spread GBPJPY | none published | same page, and the GBP/JPY market page, which shows only a live quote | — | **excluded (uncosted)** |
| Commission, all five | 0 | same page: "There's no commission to pay" | current | verified |
| Financing EURUSD, GBPJPY, XAUUSD | none charged | [Costs and Charges Information & Examples](https://eu-assets.contentstack.com/v3/assets/bltd74737b907a31bbb/bltc19314db598b7ab0/UK_Legal_Costs_and_Charges.pdf) (v9.0): the funding fee is a mark-up of up to 3% on the tom-next points | September 2026 | excluded (omitted) |
| Financing indices | 2.5% /360, long and short | same document: "(ARR +/- admin fee)/360", "Admin fee is 2.5%" | September 2026 | verified |

Four instruments are **costed**, all on published minimums (a lower bound).
**GBPJPY is uncosted** at this venue.

**Pepperstone Razor (CFD)**

| Component | Figure | Source | Date | Status |
|---|---|---|---|---|
| Spread EURUSD | 0.1 average (minimum 0.0) | [Costs and Charges Information & Examples](https://eu-assets.contentstack.com/v3/assets/bltd74737b907a31bbb/bltc19314db598b7ab0/UK_Legal_Costs_and_Charges.pdf) | September 2026 | verified, average |
| Spread GBPJPY | 1.20 average (minimum 0.4) | [Costs and charges, March 2020](https://files.pepperstone.com/Pepperstone-Limited-Cost-and-Charges.pdf) | March 2020 | verified, average |
| Spread XAUUSD | 22 ($0.22) average (minimum $0.10); the account is not named | Costs and Charges Information & Examples | September 2026 | verified, average |
| Spread USA500IDXUSD | New York: 0.4 from 09:30 to 16:00, 1.5 from 17:00 to 18:00, 0.6 otherwise | Costs and charges, March 2020 (session minimums in server time = New York + 7h); 0.4 also on [Trading costs and fees](https://pepperstone.com/en-gb/trading/costs-and-fees) | March 2020 | verified, minimum, schedule |
| Spread DEUIDXEUR | 1.2 average (minimum 0.9) | Costs and Charges Information & Examples | September 2026 | verified, average |
| Commission FX | GBP 2.25 per 100,000 units per side | same document, MT4/MT5 GBP account | September 2026 | verified |
| Commission XAUUSD | GBP 2.25 per 100 oz lot per side | same document ("100 ounces" per lot) | September 2026 | verified |
| Commission indices | 0 | same document: index commission "reflected in the spread" | September 2026 | verified |
| Financing EURUSD, GBPJPY, XAUUSD | none charged | same document (the mark-up on tom-next) | September 2026 | excluded (omitted) |
| Financing indices | 2.5% /360, long and short | same document | September 2026 | verified |

All five Razor instruments are **costed**. USA500IDXUSD is costed on session
minimums, so its spread cost is a lower bound.

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
(non-excess) kurtosis. These match the Python reference's `dsr.py::summarise_returns`.
**Secondary:** annualised volatility `std × sqrt(252)`, and Sortino
`sqrt(252) × mean(excess) / sqrt(mean(min(excess, 0)²))` with the downside taken
over every observation.

### Drawdown

The drawdown is measured peak to trough on the daily curve, with the starting
capital as the opening point (dated the day before the window). There are two
maxima, because the deepest loss in pounds and in percent can be different
episodes: `by_amount` (`equity − running peak`, first-class for the ~£100k
capital ceiling) and `by_fraction` (`equity / running peak − 1`). Each records
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
  - `Exit { reason }`.

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
   - a **stop move** goes to `set_levels`.
4. With a position still open, `accrue_financing` runs at the bar close.

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
  `EngineStats::final_bar_entries` counts such entries.
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
    fn base_config(&self, base: BaseConfig) -> Result<Value, String>;
    /// Build the strategy from a resolved config, refusing unknown fields.
    fn build(&self, config: Value) -> Result<BoxedStrategy, String>;
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
name = "a label"                           # not part of any id
strategy = "my_strategy"                   # a name in the StrategyRegistry
base = "default"                           # | frozen (the default), if the factory provides one
instruments = ["XAUUSD"]                   # each is crossed with every parameter cell
first_day = "2024-01-01"                   # inclusive UTC days
last_day = "2024-01-31"
warmup_days = 90                           # calendar days loaded before first_day
venue = "ig_spread_bet"
# cost_config = "path/to/costs.toml"       # absent: the builtin config/costs.toml
exit_evaluation = { mode = "intrabar", both_hit = "stop_first" }
account_fx = { account_currency = "GBP", rates = { USD = 0.79 } }
sizing = { kind = "stake_per_point", stake = 0.1 }
end_of_run = "close"                       # | leave_open
starting_capital = 25000.0
risk_free_rate = 0.0

[grid]                                     # or [[cells]], an explicit list
period = [10, 20, 40]
take_profit = [8.0, 12.0]
"filter.confirm_on" = [2]                  # dotted paths reach nested fields
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
  `raw_scale_path`, which is derived.

A value the strategy rejects becomes that cell's recorded failure.

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
| `sweep` | `schema_version` (1), `sweep_id`, `code_version`, `code_revision`, `spec`, `cost_config` `{source, blake3}`, `run` (`RunMetadata`), `cells`, `started_utc` |
| `trial` | `schema_version`, `trial_id`, `sweep_id`, `index`, `code_version`, `code_revision`, `strategy`, `instrument`, `params` (the cell), `config` (the resolved strategy config, `null` if it did not resolve), `first_day`, `last_day`, `warmup_days`, `venue`, `cost_config`, `exit_evaluation`, `account_fx`, `sizing`, `end_of_run`, `starting_capital`, `risk_free_rate`, `run` (`RunMetadata`: placeholders, exclusions and the derived `costed` map), `started_utc`, `finished_utc`, `outcome` |

`outcome` is one of:
- `{status: "ok", engine: EngineStats, metrics: Metrics, trades}`, with the full
  metrics, the equity curve and every closed trade (`ClosedTrade`, in closing order;
  the walk-forward attributes them to its test windows). A ruined run is `ok`, and its
  metrics say it was ruined. A record written before task #62 has no `trades` and reads
  back with `None`;
- `{status: "failed", stage: "config" | "run" | "panic", error}`.

The ids are blake3 hashes:
- **`trial_id`** hashes the canonical JSON of everything the result depends on:
  the code version (`CARGO_PKG_VERSION`) and build (`code_revision`), the
  strategy, the params and resolved config, the instrument, the window and
  warm-up, the venue, the blake3 of the cost config's TOML text, the exit mode,
  the FX, the sizing, the end-of-run rule, the capital and the risk-free rate.
- **`code_revision`** is `tradedesk_data::CODE_REVISION`: the git commit the
  workspace was built from, prefixed `dirty-` for a modified tree, or `unknown`
  without git. Two builds of one release therefore never share an id, and a
  sweep re-run on newer code is not deduplicated against stale results. Ids
  written before the build was hashed (M1) differ from today's for the same
  inputs, and those records read back with `code_revision` absent.
- The sweep's other cells and its `name` are not in the trial id, so a trial
  keeps its id when the grid grows.
- **`sweep_id`** hashes the code version and build, the whole spec and the cost
  hash.

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

The assumptions follow the Python reference implementation (`dsr.py`) the
goldens were generated from:
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

This mirrors the Python reference's `cscv.py::pbo_cscv`, except that the purge
(`label_horizon`) and the embargo default to 0, which is the paper's plain CSCV.
Daily returns do not overlap, so there is nothing to purge. The Python default
(`label_horizon = 1`) is available through `CscvConfig`.

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
my-backtest sweep spec.toml --out registry.jsonl --cache-root path/to/marketdata
tradedesk-backtest report registry.jsonl
tradedesk-backtest report registry.jsonl --format json
tradedesk-backtest walkforward registry.jsonl
```

`walkforward` needs a window longer than its train window (24 months by default);
a shorter one finds no fold and exits 1.

`--cache-root` may come from `TRADEDESK_CACHE_ROOT` instead.

| Command | What it does |
|---|---|
| `sweep <SPEC> --out <REGISTRY> --cache-root <DIR>` | Validates the spec (the format in "The spec"), opens the registry for appending (created if absent), loads the data once and runs every cell |
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
| 1 | Input refused before anything ran: the spec is unreadable or invalid, the cost config is invalid, or the registry is unreadable, truncated (`TruncatedTail`), of an unknown schema, or has no single sweep to report. For `walkforward`, also invalid months or a window too short for one fold. A refused spec creates no registry file |
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
over the join and aggregation, the loader, fills, financing, stops and targets,
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
  tradedesk's Python indicators; `tests/fixtures/metrics_golden.json` pins the
  Sharpe, volatility, Sortino, skewness, kurtosis and both maximum drawdowns
  against pandas/numpy; `tests/fixtures/overfit_golden.json` pins the deflated
  Sharpe and the PBO against the Python reference implementation. Each fixture
  records its provenance (the generating command and library versions); the
  generators ran in a private checkout and are not part of this repository.
- **Command line.** The shipped binary registers no strategy, so the sweep paths
  run in-process through `cli::run` with the test strategy registered; `report`,
  `walkforward`, usage errors, SIGINT and the empty-registry refusal run the
  built binary.
