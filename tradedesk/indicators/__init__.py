# tradedesk/indicators/__init__.py
"""
Technical indicators for trading strategies.

Provides stateful indicator classes that can be updated with new candles
and return calculated values.

Example:
    from tradedesk.indicators import WilliamsR, MFI, MACD
    
    wr = WilliamsR(period=14)
    mfi = MFI(period=14)
    macd = MACD(fast=12, slow=26, signal=9)
    
    # Update with each new candle
    wr_value = wr.update(candle)
    mfi_value = mfi.update(candle)
    macd_values = macd.update(candle)
"""

from .base import Indicator
from .williams_r import WilliamsR
from .mfi import MFI
from .macd import MACD

__all__ = ["Indicator", "WilliamsR", "MFI", "MACD"]
