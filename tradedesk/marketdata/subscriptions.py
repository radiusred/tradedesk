from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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
        period: The candle period. Common values include "1MINUTE", "5MINUTE",
            "15MINUTE", "30MINUTE", "HOUR", "4HOUR", "DAY", "WEEK".
        fields: An optional list of custom provider-specific fields to subscribe
            to. If `None`, a default set of OHLCV and volume fields is used.

    Example:
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE"),
            ChartSubscription("CS.D.EURUSD.TODAY.IP", "1MINUTE"),
        ]
    """

    period: str
    fields: list[str] | None = field(default=None)

    def __post_init__(self) -> None:
        """Set default fields if not provided."""
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
