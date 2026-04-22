"""Public recording exports for ledgers, metrics, and reporting helpers."""

# Events
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
    build_candle_index,
    compute_excursions,
)

# Ledger
from .ledger import TradeLedger, trade_rows_from_trades

# Loader
from .loader import load_trades_from_backtest

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
    # Loader
    "load_trades_from_backtest",
    # Metrics
    "Metrics",
    "RoundTrip",
    "compute_metrics",
    "round_trips_from_fills",
    "equity_rows_from_round_trips",
    # Excursions
    "CandleIndex",
    "build_candle_index",
    "compute_excursions",
    # Recorders
    "EquityRecorder",
    "ExcursionComputer",
    "ProgressLogger",
    "TrackerSync",
    # Subscriber
    "register_recording_subscriber",
]
