# tradedesk/execution/ig/metadata.py
"""IG instrument metadata caching."""
from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import IGClient

log = logging.getLogger(__name__)


class IGMetadataCache:
    """Fetches and caches IG instrument/market metadata and dealing rules."""

    _PERIOD_MAP: dict[str, str] = {
        "1MINUTE": "MINUTE",
        "2MINUTE": "MINUTE_2",
        "3MINUTE": "MINUTE_3",
        "5MINUTE": "MINUTE_5",
        "10MINUTE": "MINUTE_10",
        "15MINUTE": "MINUTE_15",
        "30MINUTE": "MINUTE_30",
        "60MINUTE": "HOUR",
        "120MINUTE": "HOUR_2",
        "180MINUTE": "HOUR_3",
        "240MINUTE": "HOUR_4",
        "1440MINUTE": "DAY",
        "HOUR": "HOUR",
        "2HOUR": "HOUR_2",
        "3HOUR": "HOUR_3",
        "4HOUR": "HOUR_4",
        "DAY": "DAY",
        "WEEK": "WEEK",
        "MONTH": "MONTH",
        # Pass IG-native formats through unchanged
        "SECOND": "SECOND",
        "MINUTE": "MINUTE",
        "MINUTE_2": "MINUTE_2",
        "MINUTE_3": "MINUTE_3",
        "MINUTE_5": "MINUTE_5",
        "MINUTE_10": "MINUTE_10",
        "MINUTE_15": "MINUTE_15",
        "MINUTE_30": "MINUTE_30",
        "HOUR_2": "HOUR_2",
        "HOUR_3": "HOUR_3",
        "HOUR_4": "HOUR_4",
    }

    def __init__(self, client: IGClient) -> None:
        self._client = client
        self._cache: dict[str, dict[str, Any]] = {}

    def period_to_rest_resolution(self, period: str) -> str:
        """Map tradedesk period strings to IG REST resolution strings."""
        return self._PERIOD_MAP.get(period.upper(), period.upper())

    async def get_market_snapshot(self, epic: str) -> dict[str, Any]:
        """Return the latest market snapshot for the given epic."""
        return await self._client._request("GET", f"/markets/{epic}")

    async def get_instrument_metadata(
        self, epic: str, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Fetch and cache instrument metadata (dealing rules) for the given epic."""
        if not force_refresh and epic in self._cache:
            return self._cache[epic]
        metadata = await self.get_market_snapshot(epic)
        self._cache[epic] = metadata
        return metadata

    async def quantise_size(self, epic: str, size: float) -> float:
        """Quantise position size to the instrument's minimum step."""
        # Call via client so tests that monkeypatch client.get_instrument_metadata work
        metadata = await self._client.get_instrument_metadata(epic)
        dealing_rules = metadata.get("dealingRules") or {}
        min_value = (dealing_rules.get("minDealSize") or {}).get("value")

        if min_value is None:
            log.warning(
                "No minDealSize in dealing rules for %s — falling back to 2 dp rounding",
                epic,
            )
            return round(float(size), 2)

        min_str = str(min_value)
        decimal_places = len(min_str.split(".")[1]) if "." in min_str else 0
        step = Decimal(10) ** -decimal_places

        s = Decimal(str(size))
        quantised = float((s / step).to_integral_value(rounding=ROUND_DOWN) * step)

        if quantised < float(min_value):
            quantised = float(min_value)

        if quantised != size:
            log.debug(
                "Quantised size for %s: %.10f -> %.10f (step=%.10f, min=%.10f)",
                epic,
                size,
                quantised,
                float(step),
                float(min_value),
            )

        return quantised
