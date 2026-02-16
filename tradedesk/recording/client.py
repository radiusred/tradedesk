from typing import Any

from tradedesk.time_utils import now_utc_iso

from .ledger import TradeLedger
from .types import TradeRecord


class RecordingClient:
    """
    Transparent client wrapper that records executions.

    - Delegates all attributes/methods to the wrapped client.
    - Intercepts place_market_order to append a TradeRecord to the ledger.

    This keeps recording client-agnostic and avoids touching tradedesk/backtest internals.
    """

    def __init__(self, inner: Any, *, ledger: TradeLedger):
        self._inner = inner
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else
        return getattr(self._inner, name)

    def _current_timestamp(self) -> str:
        # BacktestClient maintains this; broker clients may later expose something similar.
        ts = getattr(self._inner, "_current_timestamp", None)
        if isinstance(ts, str) and ts:
            return ts
        # If the inner client doesn't provide a timestamp, fall back to now (UTC).
        # Returning a valid ISO timestamp prevents downstream parsers from
        # raising on empty strings (e.g. datetime.fromisoformat('')).
        return now_utc_iso()

    async def place_market_order(
        self,
        instrument: str,
        direction: str,
        size: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resp: dict[str, Any] = await self._inner.place_market_order(
            instrument=instrument, direction=direction, size=size, **kwargs
        )
        self._record_trade(
            instrument=instrument,
            direction=direction,
            size=size,
            price=resp.get("price") or 0.0,
            reason="market_order",
        )
        return resp

    async def place_market_order_confirmed(
        self,
        instrument: str,
        direction: str,
        size: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        resp: dict[str, Any] = await self._inner.place_market_order_confirmed(
            instrument=instrument, direction=direction, size=size, **kwargs
        )
        self._record_trade(
            instrument=instrument,
            direction=direction,
            size=size,
            price=resp.get("price") or 0.0,
            reason="market_order",
        )
        return resp

    def _record_trade(
        self,
        instrument: str,
        direction: str,
        size: float,
        price: float | None,
        reason: str,
    ) -> None:
        if price is None or price == 0.0:
            # fallback to mark price if available
            get_mark = getattr(self._inner, "get_mark_price", None)
            mark_value = get_mark(instrument) if callable(get_mark) else None
            price = float(mark_value) if mark_value is not None else 0.0

        ts = self._current_timestamp()
        self._ledger.record_trade(
            TradeRecord(
                timestamp=ts,
                instrument=instrument,
                direction=direction,
                size=float(size),
                price=float(price),
                reason=reason,
            )
        )
