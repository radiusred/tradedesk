"""
Provider-neutral interfaces.

The intent is to keep tradedesk strategies independent from any single broker.
At this stage the interfaces are intentionally small; we will extend them as
we encapsulate streaming and implement backtesting.
"""

from dataclasses import dataclass

# Re-export Direction for backward compatibility
__all__ = [
    "AccountBalance",
    "BrokerPosition",
    "DealRejectedException",
    "HistoricalDataAllowanceError",
]


@dataclass(frozen=True)
class BrokerPosition:
    """Provider-neutral representation of a position held at the broker."""

    instrument: str
    direction: str  # "BUY" or "SELL" (broker-native)
    size: float
    entry_price: float
    deal_id: str
    currency: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class AccountBalance:
    """Provider-neutral snapshot of account funds."""

    balance: float  # total account value
    deposit: float  # margin used
    available: float  # funds available for new positions
    profit_loss: float  # unrealised P&L
    currency: str = ""


class DealRejectedException(Exception):
    """Raised when a deal is not accepted after placing a market order."""

    pass


class HistoricalDataAllowanceError(RuntimeError):
    """Raised when IG returns HTTP 403 with exceeded-account-historical-data-allowance."""

    pass
