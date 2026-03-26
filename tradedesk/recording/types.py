from dataclasses import dataclass
from enum import Enum


class RecordingMode(Enum):
    """Recording mode: BACKTEST or BROKER.

    ARCHITECTURE NOTE: This enum is a known architecture smell. The recording
    domain should NOT know about execution context (backtest vs live). However,
    the modes represent fundamentally different data availability:

    - BACKTEST: Has full equity history via record_equity() calls
    - BROKER: Must compute synthetic equity from position tracking

    Future refactoring should split this into orthogonal concerns:
    - write_mode: BATCH vs INCREMENTAL
    - equity_source: EXTERNAL vs SYNTHETIC
    - output_files: FULL vs MINIMAL

    For now, keep as-is since it works for two well-defined use cases.
    """

    BACKTEST = "backtest"
    BROKER = "broker"  # covers both demo and live


@dataclass(frozen=True)
class TradeRecord:
    timestamp: str
    instrument: str
    direction: str  # "BUY" or "SELL"
    size: float  # stake (e.g. £/point)
    price: float  # executable fill price (ask for BUY, bid for SELL, with overlays applied)
    reason: str = ""
    # Cost decomposition — all zero when bid/ask data is unavailable or bid/ask mode is disabled
    raw_price: float = 0.0  # mid price at fill time
    spread_cost: float = 0.0  # half-spread charged (price units)
    slippage_cost: float = 0.0  # fixed + proportional slippage (price units)
    commission_cost: float = 0.0  # commission charged (monetary units, e.g. £)


@dataclass(frozen=True)
class EquityRecord:
    timestamp: str
    equity: float
