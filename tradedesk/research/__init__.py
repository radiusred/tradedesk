"""Research methodology tooling.

Walk-forward evaluation and correlation gating built on top of the
event-driven backtest runner. These tools automate the IS/OOS slicing and
LIVE-correlation checks that researchers previously did by hand.
"""

from .correlation import (
    CorrelationResult,
    correlation_gate,
    daily_pnl_from_csv,
    daily_pnl_from_round_trips,
    daily_pnl_from_trade_rows,
    pearson,
)
from .walkforward import (
    WalkForwardReport,
    WalkForwardSpec,
    WalkForwardWindow,
    WindowResult,
    run_walk_forward,
    walk_forward_windows,
)

__all__ = [
    "CorrelationResult",
    "WalkForwardReport",
    "WalkForwardSpec",
    "WalkForwardWindow",
    "WindowResult",
    "correlation_gate",
    "daily_pnl_from_csv",
    "daily_pnl_from_round_trips",
    "daily_pnl_from_trade_rows",
    "pearson",
    "run_walk_forward",
    "walk_forward_windows",
]
