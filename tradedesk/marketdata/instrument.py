"""Instrument identity and tick-level ``MarketData`` value types.

``Instrument`` is the immutable, broker-agnostic representation of a tradable
asset (symbol plus optional ISIN/RIC/name/asset class and a per-broker code
map, with ISIN format and Luhn checksum validation). ``MarketData`` is a
lightweight value object carrying a single bid/offer tick update.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Instrument:
    """
    A standardized, immutable representation of a tradable financial asset.

    This class serves as a universal identifier to replace broker-specific
    strings (such as IG Group's 'EPIC'). It centralizes common identifiers
    like ISIN and RIC while allowing for a mapping of specific codes used
    by different provider APIs.

    Attributes:
        symbol: The primary ticker or shorthand identifier (e.g., 'AAPL' or 'GBPUSD').
            This is the only required field.
        isin: The International Securities Identification Number (12-character code).
        ric: The Reuters Instrument Code (e.g., 'AAPL.O').
        name: The full human-readable name of the instrument (e.g., 'Apple Inc').
        asset_class: The category of the instrument (e.g., 'FX', 'Equity', 'Future').
        broker_codes: A mapping of broker names to their specific internal identifiers.
            Example: {'ig': 'CS.D.GBPUSD.TODAY.IP', 'ibkr': 'GBP.USD'}

    Example:
        >>> aapl = Instrument(symbol="AAPL", isin="US0378331005", asset_class="Equity")
        >>> print(aapl)
        AAPL
    """

    symbol: str
    isin: Optional[str] = None
    ric: Optional[str] = None
    name: Optional[str] = None
    asset_class: Optional[str] = None
    # We use a default_factory because a dict is a mutable object.
    # This ensures every instance gets its own unique dictionary.
    broker_codes: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """
        Validates the instrument. Checks ISIN format and checksum.
        """
        if self.isin is not None:
            self._validate_isin(self.isin)

    def _validate_isin(self, isin: str) -> None:
        """
        Validates the ISIN format and checksum using the Luhn algorithm.
        Raises ValueError if invalid.
        """
        # 1. Basic Format Check: 2 letters, 9 alphanumeric, 1 digit
        if not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            raise ValueError(
                f"Invalid ISIN format for {self.symbol}: '{isin}'. "
                "Expected 2 letters, 9 alphanumeric, and 1 digit."
            )

        # 2. Convert letters to digits (A=10, B=11, ..., Z=35)
        digits_str = "".join(str(int(char, 36)) if char.isalpha() else char for char in isin)

        # 3. Apply Luhn Algorithm
        digits = [int(d) for d in digits_str]
        checksum_sum = 0

        for i, digit in enumerate(reversed(digits)):
            if i % 2 == 1:
                doubled = digit * 2
                checksum_sum += doubled if doubled < 10 else doubled - 9
            else:
                checksum_sum += digit

        if checksum_sum % 10 != 0:
            raise ValueError(f"ISIN checksum failed for {self.symbol}: '{isin}'.")

    def __str__(self) -> str:
        return self.symbol

    def __repr__(self) -> str:
        return f"Instrument(symbol={self.symbol!r}, isin={self.isin!r})"


@dataclass(frozen=True)
class MarketData:
    """Represents a tick-level market update."""

    instrument: str
    bid: float
    offer: float
    timestamp: str
    raw: dict[str, Any]
