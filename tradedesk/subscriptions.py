# tradedesk/subscriptions.py
"""
Subscription type definitions for market data streams.

Strategies declare their data needs using these subscription types,
and the framework handles the Lightstreamer subscription setup.
"""

from dataclasses import dataclass, field


@dataclass
class MarketSubscription:
    """
    Subscribe to live tick-by-tick price updates for an instrument.
    
    Used for real-time bid/offer monitoring without candle aggregation.
    Triggers strategy's on_price_update() callback.
    
    Example:
        SUBSCRIPTIONS = [
            MarketSubscription("CS.D.GBPUSD.TODAY.IP"),
        ]
    """
    epic: str
    
    def get_item_name(self) -> str:
        """Returns Lightstreamer item name format."""
        return f"MARKET:{self.epic}"
    
    def get_fields(self) -> list[str]:
        """Returns Lightstreamer fields to subscribe to."""
        return ["UPDATE_TIME", "BID", "OFFER", "MARKET_STATE"]


@dataclass
class ChartSubscription:
    """
    Subscribe to OHLCV candle data for an instrument at a specific timeframe.
    
    Triggers strategy's on_candle_update() callback when candles complete.
    
    Args:
        epic: The instrument identifier
        period: Candle period - one of:
            "1MINUTE", "5MINUTE", "15MINUTE", "30MINUTE",
            "HOUR", "4HOUR", "DAY", "WEEK"
        fields: Optional custom field list (uses sensible defaults if None)
    
    Example:
        SUBSCRIPTIONS = [
            ChartSubscription("CS.D.GBPUSD.TODAY.IP", "5MINUTE"),
            ChartSubscription("CS.D.EURUSD.TODAY.IP", "1MINUTE"),
        ]
    """
    epic: str
    period: str
    fields: list[str] = field(default=None)
    
    def __post_init__(self):
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
                "LTV",              # Last traded volume
                "CONS_TICK_COUNT",  # Consolidated tick count (volume proxy)
                # Metadata
                "CONS_END",         # Candle completion indicator
                "UTM",              # Update timestamp
            ]
    
    def get_item_name(self) -> str:
        """Returns Lightstreamer item name format."""
        return f"CHART:{self.epic}:{self.period}"
    
    def get_fields(self) -> list[str]:
        """Returns Lightstreamer fields to subscribe to."""
        return self.fields
    
    def get_lightstreamer_period(self) -> str:
        """
        Convert period to Lightstreamer format if needed.
        
        IG's Lightstreamer uses periods like "MINUTE_1", "MINUTE_5", etc.
        This handles common aliases.
        """
        # Map common formats to IG format
        period_map = {
            "1MINUTE": "MINUTE_1",
            "5MINUTE": "MINUTE_5",
            "15MINUTE": "MINUTE_15",
            "30MINUTE": "MINUTE_30",
            "HOUR": "HOUR_1",
            "4HOUR": "HOUR_4",
            "DAY": "DAY",
            "WEEK": "WEEK",
            # Also accept IG format directly
            "MINUTE_1": "MINUTE_1",
            "MINUTE_5": "MINUTE_5",
            "MINUTE_15": "MINUTE_15",
            "MINUTE_30": "MINUTE_30",
            "HOUR_1": "HOUR_1",
            "HOUR_4": "HOUR_4",
        }
        
        return period_map.get(self.period.upper(), self.period)
