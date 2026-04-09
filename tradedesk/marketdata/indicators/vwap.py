"""Volume Weighted Average Price (VWAP) indicator implementation."""

from datetime import datetime, timedelta, timezone

from tradedesk.types import Candle

from .base import Indicator


def _parse_utc_dt(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string to a UTC datetime."""
    ts = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class VWAP(Indicator):
    """
    Session VWAP (defaults to UTC day sessions).

    VWAP = sum(price * volume) / sum(volume)

    Price basis:
      - Uses typical price (H+L+C)/3 by default
      - Optional: use close-only

    Session reset:
      - By default, resets when the UTC date (YYYY-MM-DD) changes in the candle timestamp.
      - When ``reset_hour_utc`` is set, resets when the candle crosses that
        UTC hour boundary instead (e.g. 7 for London open).  This takes
        precedence over ``reset_daily_utc``.
      - Assumes candle timestamps are ISO8601 strings.
    """

    def __init__(
        self,
        *,
        use_typical_price: bool = True,
        reset_daily_utc: bool = True,
        reset_hour_utc: int | None = None,
    ):
        self.use_typical_price = bool(use_typical_price)
        self.reset_daily_utc = bool(reset_daily_utc)
        self.reset_hour_utc = reset_hour_utc

        self._session_key: str | None = None
        self._cum_pv: float = 0.0
        self._cum_v: float = 0.0

    def _session_key_for(self, ts: str) -> str:
        """Derive the session key from a candle timestamp."""
        if self.reset_hour_utc is not None:
            dt = _parse_utc_dt(ts)
            hour = self.reset_hour_utc
            # Session starts at reset_hour_utc; a candle before that hour
            # belongs to the previous day's session.
            if dt.hour < hour:
                dt = dt.replace(hour=0) - timedelta(days=1)
            return f"{dt.strftime('%Y-%m-%d')}T{hour:02d}"
        return ts[:10]  # "YYYY-MM-DD"

    def update(self, candle: Candle) -> float | None:
        ts = str(candle.timestamp)
        if self.reset_hour_utc is not None or self.reset_daily_utc:
            session_key = self._session_key_for(ts)
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
