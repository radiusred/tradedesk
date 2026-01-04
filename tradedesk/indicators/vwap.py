"""Volume Weighted Average Price (VWAP) indicator implementation."""

from tradedesk.marketdata import Candle
from .base import Indicator


class VWAP(Indicator):
    """
    Session VWAP (defaults to UTC day sessions).

    VWAP = sum(price * volume) / sum(volume)

    Price basis:
      - Uses typical price (H+L+C)/3 by default
      - Optional: use close-only

    Session reset:
      - By default, resets when the UTC date (YYYY-MM-DD) changes in the candle timestamp.
      - Assumes candle timestamps are ISO8601 strings with a leading 'YYYY-MM-DD'.
    """

    def __init__(self, *, use_typical_price: bool = True, reset_daily_utc: bool = True):
        self.use_typical_price = bool(use_typical_price)
        self.reset_daily_utc = bool(reset_daily_utc)

        self._session_key: str | None = None
        self._cum_pv: float = 0.0
        self._cum_v: float = 0.0

    def update(self, candle: Candle) -> float | None:
        ts = str(candle.timestamp)
        if self.reset_daily_utc:
            session_key = ts[:10]  # "YYYY-MM-DD"
            if self._session_key is None:
                self._session_key = session_key
            elif session_key != self._session_key:
                self.reset()
                self._session_key = session_key

        vol = float(candle.volume)
        if vol < 0:
            raise ValueError("volume must be >= 0")

        if self.use_typical_price:
            price = (float(candle.high) + float(candle.low) + float(candle.close)) / 3.0
        else:
            price = float(candle.close)

        self._cum_pv += price * vol
        self._cum_v += vol

        if self._cum_v == 0.0:
            return None

        return self._cum_pv / self._cum_v

    def ready(self) -> bool:
        return self._cum_v > 0.0

    def reset(self) -> None:
        self._cum_pv = 0.0
        self._cum_v = 0.0
        # Keep _session_key; it is managed by update() when reset_daily_utc is enabled.

    def warmup_periods(self) -> int:
        # First candle with non-zero volume yields a value.
        return 1
