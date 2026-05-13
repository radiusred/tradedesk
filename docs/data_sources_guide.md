# External data sources guide

`tradedesk.data_sources` covers datasets that are neither live market streams
nor Dukascopy candle cache files.

The current public module is CFTC Commitment of Traders (COT) history.

## What the module provides

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

Commodity contracts use the CFTC disaggregated futures report. `SP500` and
`TNOTE10` use the Traders in Financial Futures (TFF) report.

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
