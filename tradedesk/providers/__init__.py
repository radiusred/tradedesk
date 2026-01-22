"""
Provider-agnostic interfaces.

This module defines the stable interfaces used by strategies and runners.
Concrete provider implementations (e.g. IG) should implement these contracts.
"""

from ..marketdata import MarketData
from .base import Client, Streamer
from ..marketdata import CandleClose

__all__ = [
    "Client",
    "Streamer",
    "MarketData",
    "CandleClose",
]
