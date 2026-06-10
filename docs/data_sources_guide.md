# External data sources guide

`tradedesk.data_sources` covers datasets that are neither live market streams
nor Dukascopy candle cache files.

Three free, no-auth macro feeds are supported:

- **FRED** (`fred`) — US rates + VIX from the St. Louis Fed.
- **ECB** (`ecb`) — EUR €STR, AAA government yield curve and Euribor from the
  ECB Data Portal.
- **CFTC COT** (`cot`) — Commitment-of-Traders positioning.

The [Macro Parquet lake](#macro-parquet-lake) section describes how all three
are materialized to Parquet and loaded in backtests via a single access
pattern (`load_macro_series` / `load_macro_frame`). The sections below the lake
document the lower-level CFTC COT parsing API in detail.

## Macro Parquet lake

Macro series are slow tabular time series, so each series is materialized to a
single typed Parquet file under the existing market-data lake (the same root
used by the Dukascopy OHLCV cache):

```
<lake>/macro/fred/<SERIES_ID>.parquet     # e.g. DGS10.parquet
<lake>/macro/ecb/<LABEL>.parquet          # e.g. EUR_YLD_2Y.parquet
<lake>/macro/cftc/<LABEL>.parquet         # e.g. EURUSD.parquet, GOLD.parquet
```

`<lake>` defaults to `$TRADEDESK_MARKETDATA` or `/paperclip/tradedesk/marketdata`.

### Ingest (on demand or weekly)

```bash
# Everything (FRED + ECB + CFTC) since 2010 into the default lake
python -m tradedesk.data_sources.ingest

# One source, custom lake, custom start
python -m tradedesk.data_sources.ingest --source cftc --lake /data/marketdata --from 2018-01-01
```

Ingestion is idempotent — re-running refreshes each series in place and
re-downloads the current-year CFTC zip so the latest Friday release appears.
Failures are isolated at two levels so a weekly cron stays green when an
upstream endpoint is briefly unavailable. A failure on a single series — including
read timeouts and dropped connections — is logged and skipped (never fatal), and
the prior parquet for that series is left in place. On top of that, an entirely
dead source is isolated: if FRED is down, ECB and CFTC still materialize, and the
run returns the surviving sources rather than aborting. Run it weekly (CFTC
publishes Fridays 15:30 ET; a Saturday run captures the new week).

### Load (the backtest access pattern)

```python
from tradedesk.data_sources import load_macro_series, load_macro_frame

# One series → DatetimeIndex 'date' + a 'value' column (FRED/ECB)
dgs10 = load_macro_series("FRED", "DGS10")
estr = load_macro_series("ECB", "EUR_ESTR")

# Several series of one source → wide DataFrame, one column per label
rates = load_macro_frame("FRED", ["DGS2", "DGS10", "VIXCLS"])

# CFTC → Tuesday-indexed positioning frame (release_date, open_interest,
# commercial/dealer/asset_mgr/leveraged long/short/net)
eur_cot = load_macro_series("CFTC", "EURUSD")
```

### Series catalog

| Source | Labels | Frequency | Notes |
| ------ | ------ | --------- | ----- |
| FRED | `DFF`, `DGS3MO`, `DGS2`, `DGS10`, `T10Y2Y`, `VIXCLS` | daily | Effective fed funds, Treasury yields, 10Y-2Y spread, VIX |
| ECB | `EUR_ESTR` | daily | €STR — the EUR OIS reference (overnight) rate |
| ECB | `EUR_YLD_3M/6M/1Y/2Y/5Y/10Y` | daily | AAA government yield-curve spot rates |
| ECB | `EUR_EURIBOR_1M`, `EUR_EURIBOR_3M` | monthly | Euribor term money-market rates |
| CFTC | `GOLD`, `SILVER`, `WTI`, `BRENT`, `NATGAS`, `SP500`, `TNOTE10`, `EURUSD`, `JPYUSD`, `GBPUSD` | weekly | COT positioning; index on report-Tuesday |

**On EUR OIS:** the ECB does not publish term OIS swap fixings (1w/1m/3m) as a
free single series. `EUR_ESTR` is the overnight OIS reference and the short-end
yield-curve tenors give the market-implied term structure; together they are
the free, no-auth proxies for OIS-implied magnitude in a rate-surprise study.

**No look-ahead with CFTC:** the `date` index is the Tuesday as-of date, which
is *not* public until the Friday `release_date`. Backtests must gate entries
off `release_date` (or the first trading day after it), never the index.

## What the module provides (CFTC COT detail)

Import from `tradedesk.data_sources`:

```python
from tradedesk.data_sources import (
    CFTC_CONTRACTS,
    CFTCReport,
    cot_release_date,
    download_cot_zip,
    iter_cot_rows,
    load_contract_history,
)
```

Public API:

- `CFTC_CONTRACTS`: built-in short-label to CFTC contract mapping
- `CFTCReport`: report family enum (`DISAGGREGATED` or `TFF`)
- `download_cot_zip(...)`: download and cache raw annual CFTC zip archives
- `iter_cot_rows(...)`: stream parsed `COTRow` objects from an archive set
- `load_contract_history(...)`: load a sorted weekly history for one contract
- `cot_release_date(...)`: map the CFTC Tuesday as-of date to the scheduled Friday release date

## Contract map

The built-in contract map currently covers:

- `GOLD`
- `SILVER`
- `WTI`
- `BRENT`
- `NATGAS`
- `SP500`
- `TNOTE10`
- `EURUSD` (CME Euro FX)
- `JPYUSD` (CME Japanese Yen)
- `GBPUSD` (CME British Pound)

Commodity contracts use the CFTC disaggregated futures report. `SP500`,
`TNOTE10` and the CME currency futures use the Traders in Financial Futures
(TFF) report. CME FX futures are quoted as foreign-currency/USD, so a net-long
position is bullish the base currency vs the dollar.

## Loading one contract history

```python
from datetime import date
from pathlib import Path

from tradedesk.data_sources import CFTC_CONTRACTS, load_contract_history

rows = load_contract_history(
    cache_dir=Path("/tmp/tradedesk-cache"),
    contract=CFTC_CONTRACTS["GOLD"],
    date_from=date(2020, 1, 1),
    date_to=date(2020, 12, 31),
)

latest = rows[-1]
print(latest.report_date_tuesday, latest.release_date_friday, latest.commercial_net)
```

Each returned `COTRow` includes:

- `report_date_tuesday`: the CFTC as-of date
- `release_date_friday`: the scheduled publication date for that report week
- `report`: which report family produced the row
- `commercial_long`, `commercial_short`, `commercial_net`
- `open_interest`
- TFF-only buckets (`0` for disaggregated rows): `dealer_*`, `asset_mgr_*`,
  `leveraged_*` long/short/net. For TFF rows the dealer bucket is mirrored into
  `commercial_*` for backward compatibility, so FX/index trend-gating studies
  should read the explicit `asset_mgr_*` / `leveraged_*` (speculative)
  positioning rather than `commercial_*`.

## Release-date semantics

The CFTC report week closes on Tuesday and is normally published on Friday.
`cot_release_date(...)` encodes that fixed three-calendar-day offset.

```python
from datetime import date
from tradedesk.data_sources import cot_release_date

assert cot_release_date(date(2026, 1, 6)).isoformat() == "2026-01-09"
```

Strategies that trade from COT inputs should key entry timing off the release
date rather than the Tuesday as-of date to avoid look-ahead bias.

## Cache layout

Downloads are stored under `cache_dir/cftc/`. Existing zip files are reused on
subsequent calls unless `force=True` is passed to `download_cot_zip(...)`.

This cache is separate from the Dukascopy cache used by
`tradedesk.execution.backtest`.

## Related docs

- [backtesting_guide.md](backtesting_guide.md)
- [research_methodology_guide.md](research_methodology_guide.md)
- [ml_guide.md](ml_guide.md)
