# Events
# Equity functions
from .equity import compute_equity, compute_unrealised_pnl
from .events import (
    EquitySampledEvent,
    ExcursionSampledEvent,
    PositionClosedEvent,
    PositionOpenedEvent,
    ReportingCompleteEvent,
)

# Excursions
from .excursions import (
    CandleIndex,
    Excursions,
    build_candle_index,
    compute_excursions,
)

# Ledger
from .ledger import TradeLedger, trade_rows_from_trades

# Metrics
from .metrics import (
    Metrics,
    RoundTrip,
    compute_metrics,
    equity_rows_from_round_trips,
    round_trips_from_fills,
)

# Recorders (for backtest setup)
from .recorders import (
    EquityRecorder,
    ExcursionComputer,
    ProgressLogger,
    TrackerSync,
)

# Subscriber
from .subscriber import register_recording_subscriber

# Types
from .types import EquityRecord, RecordingMode, TradeRecord

__all__ = [
    # Events
    "EquitySampledEvent",
    "ExcursionSampledEvent",
    "PositionClosedEvent",
    "PositionOpenedEvent",
    "ReportingCompleteEvent",
    # Types
    "RecordingMode",
    "TradeRecord",
    "EquityRecord",
    # Ledger
    "TradeLedger",
    "trade_rows_from_trades",
    # Metrics
    "Metrics",
    "RoundTrip",
    "compute_metrics",
    "round_trips_from_fills",
    "equity_rows_from_round_trips",
    # Excursions
    "CandleIndex",
    "Excursions",
    "build_candle_index",
    "compute_excursions",
    # Recorders
    "EquityRecorder",
    "ExcursionComputer",
    "ProgressLogger",
    "TrackerSync",
    # Subscriber
    "register_recording_subscriber",
    # Equity functions
    "compute_equity",
    "compute_unrealised_pnl",
]
