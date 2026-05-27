"""External data sources used by strategies and research workflows.

Contains parsers and downloaders for off-IG market datasets — distinct from
``tradedesk.marketdata`` (live tick streaming) and
``tradedesk.execution.backtest.dukascopy`` (historic price candles).

Three free, no-auth macro feeds are supported (`RAD-2987`):

* :mod:`tradedesk.data_sources.fred` — US rates + VIX (St. Louis FRED).
* :mod:`tradedesk.data_sources.ecb` — EUR €STR, yield curve, Euribor (ECB).
* :mod:`tradedesk.data_sources.cot` — CFTC Commitment-of-Traders positioning.

:mod:`tradedesk.data_sources.lake` materializes all three to Parquet under the
market-data lake and provides :func:`load_macro_series` / :func:`load_macro_frame`
as the backtest data-access pattern.  Run ``python -m tradedesk.data_sources.ingest``
to (re)ingest on demand or on a weekly schedule.
"""

from .cot import (
    CFTC_CONTRACTS,
    CFTCContract,
    CFTCReport,
    COTRow,
    cot_release_date,
    download_cot_zip,
    iter_cot_rows,
    load_contract_history,
)
from .ecb import (
    DEFAULT_ECB_SERIES,
    ECBSeries,
    fetch_ecb_series,
    parse_ecb_csv,
)
from .fred import (
    DEFAULT_FRED_SERIES,
    fetch_fred_series,
    parse_fred_csv,
)
from .lake import (
    DEFAULT_HISTORY_START,
    MacroSource,
    available_macro_series,
    cot_history_frame,
    default_lake,
    load_macro_frame,
    load_macro_series,
    macro_dir,
    macro_path,
    materialize_all,
    materialize_cftc,
    materialize_ecb,
    materialize_fred,
)

__all__ = [
    # CFTC COT
    "CFTC_CONTRACTS",
    "CFTCContract",
    "CFTCReport",
    "COTRow",
    "cot_release_date",
    "download_cot_zip",
    "iter_cot_rows",
    "load_contract_history",
    # FRED
    "DEFAULT_FRED_SERIES",
    "fetch_fred_series",
    "parse_fred_csv",
    # ECB
    "DEFAULT_ECB_SERIES",
    "ECBSeries",
    "fetch_ecb_series",
    "parse_ecb_csv",
    # Macro lake
    "DEFAULT_HISTORY_START",
    "MacroSource",
    "available_macro_series",
    "cot_history_frame",
    "default_lake",
    "load_macro_frame",
    "load_macro_series",
    "macro_dir",
    "macro_path",
    "materialize_all",
    "materialize_cftc",
    "materialize_ecb",
    "materialize_fred",
]
