"""External data sources used by strategies and research workflows.

Contains parsers and downloaders for off-IG market datasets — distinct from
``tradedesk.marketdata`` (live tick streaming) and
``tradedesk.execution.backtest.dukascopy`` (historic price candles).
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

__all__ = [
    "CFTC_CONTRACTS",
    "CFTCContract",
    "CFTCReport",
    "COTRow",
    "cot_release_date",
    "download_cot_zip",
    "iter_cot_rows",
    "load_contract_history",
]
