"""Subscription value types describing what market data a strategy consumes.

Defines the abstract ``Subscription`` plus concrete ``MarketSubscription``
(tick-by-tick bid/offer updates) and ``ChartSubscription`` (OHLCV candles at a
chosen timeframe). Each subscription knows how to render its Lightstreamer item
name and field list.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .timeframe import Timeframe


@dataclass
class Subscription(ABC):
    """Base class for different subscription types."""

    instrument: str

    @abstractmethod
    def get_item_name(self) -> str: ...

    @abstractmethod
    def get_fields(self) -> list[str]: ...


@dataclass
class MarketSubscription(Subscription):
    """
    Subscribe to live tick-by-tick price updates for an instrument.

    Used for real-time bid/offer monitoring without candle aggregation.
    Triggers strategy's on_price_update() callback.

    Uses the IG PRICE subscription which requires an account identifier
    in the item name.

    Example:
        SUBSCRIPTIONS = [
            MarketSubscription("CS.D.GBPUSD.TODAY.IP", account_id="ABC123"),
        ]
    """

    account_id: str = ""

    def get_item_name(self) -> str:
        """Returns Lightstreamer item name format."""
        return f"PRICE:{self.account_id}:{self.instrument}"

    def get_fields(self) -> list[str]:
        """Returns Lightstreamer fields to subscribe to."""
        return ["TIMESTAMP", "BIDPRICE1", "ASKPRICE1", "DLG_FLAG"]


@dataclass
class ChartSubscription(Subscription):
    """
    Subscribe to OHLCV candle data for an instrument at a specific timeframe.

    This subscription provides a stream of completed candles, which triggers the
    strategy's `on_candle_close()` callback.

    Attributes:
        instrument: The instrument identifier (e.g., 'CS.D.GBPUSD.TODAY.IP').
        period: The candle timeframe.  Prefer :class:`Timeframe` members
            (e.g. ``Timeframe.MINUTE_5``); plain strings such as ``"5MINUTE"``,
            ``"HOUR"``, ``"DAY"`` etc. are still accepted and coerced via
            :meth:`Timeframe.from_value`.
        fields: An optional list of custom provider-specific fields to subscribe
            to. If `None`, a default set of OHLCV and volume fields is used.

    Example:
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.GBPUSD.TODAY.IP", Timeframe.MINUTE_5),
            ChartSubscription("CS.D.EURUSD.TODAY.IP", Timeframe.MINUTE_1),
        ]
    """

    period: str | Timeframe
    fields: list[str] | None = field(default=None)

    def __post_init__(self) -> None:
        """Normalise the period to a :class:`Timeframe` and set default fields."""
        # Coerce eagerly so a typo'd period string fails at construction time
        # rather than silently propagating through to Lightstreamer item names.
        # Keeping ``period`` typed as ``str | Timeframe`` (the field stays a
        # Timeframe, which IS a str via StrEnum) preserves the public string
        # API for existing callers.
        if not isinstance(self.period, Timeframe):
            self.period = Timeframe.from_value(self.period)
        if self.fields is None:
            # Standard OHLCV fields plus metadata
            self.fields = [
                # Offer (ask) prices
                "OFR_OPEN",
                "OFR_HIGH",
                "OFR_LOW",
                "OFR_CLOSE",
                # Bid prices
                "BID_OPEN",
                "BID_HIGH",
                "BID_LOW",
                "BID_CLOSE",
                # Volume data
                "LTV",  # Last traded volume
                "CONS_TICK_COUNT",  # Consolidated tick count (volume proxy)
                # Metadata
                "CONS_END",  # Candle completion indicator
                "UTM",  # Update timestamp
            ]

    def get_item_name(self) -> str:
        """Returns Lightstreamer item name format."""
        return f"CHART:{self.instrument}:{self.period}"

    def get_fields(self) -> list[str]:
        """Returns Lightstreamer fields to subscribe to."""
        assert self.fields is not None  # Set in __post_init__
        return self.fields
