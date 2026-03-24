"""Unit tests for IGMetadataCache."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.execution.ig.metadata import IGMetadataCache


def _make_client() -> MagicMock:
    client = MagicMock()
    client._request = AsyncMock()
    return client


class TestPeriodToRestResolution:
    def test_standard_mappings(self) -> None:
        client = _make_client()
        cache = IGMetadataCache(client)

        assert cache.period_to_rest_resolution("1MINUTE") == "MINUTE"
        assert cache.period_to_rest_resolution("5MINUTE") == "MINUTE_5"
        assert cache.period_to_rest_resolution("15MINUTE") == "MINUTE_15"
        assert cache.period_to_rest_resolution("30MINUTE") == "MINUTE_30"
        assert cache.period_to_rest_resolution("HOUR") == "HOUR"
        assert cache.period_to_rest_resolution("4HOUR") == "HOUR_4"
        assert cache.period_to_rest_resolution("DAY") == "DAY"
        assert cache.period_to_rest_resolution("WEEK") == "WEEK"

    def test_ig_native_passthrough(self) -> None:
        client = _make_client()
        cache = IGMetadataCache(client)
        assert cache.period_to_rest_resolution("MINUTE_5") == "MINUTE_5"
        assert cache.period_to_rest_resolution("HOUR_4") == "HOUR_4"

    def test_unknown_passthrough(self) -> None:
        client = _make_client()
        cache = IGMetadataCache(client)
        assert cache.period_to_rest_resolution("FOO") == "FOO"

    def test_case_insensitive(self) -> None:
        client = _make_client()
        cache = IGMetadataCache(client)
        assert cache.period_to_rest_resolution("1minute") == "MINUTE"


class TestGetInstrumentMetadata:
    async def test_fetches_on_miss(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealingRules": {"minDealSize": {"value": 1}}})
        cache = IGMetadataCache(client)

        result = await cache.get_instrument_metadata("EPIC1")
        assert result["dealingRules"]["minDealSize"]["value"] == 1
        client._request.assert_awaited_once_with("GET", "/markets/EPIC1")

    async def test_caches_after_first_fetch(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"data": "fresh"})
        cache = IGMetadataCache(client)

        await cache.get_instrument_metadata("EPIC1")
        await cache.get_instrument_metadata("EPIC1")
        assert client._request.await_count == 1

    async def test_force_refresh_bypasses_cache(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"data": "fresh"})
        cache = IGMetadataCache(client)

        await cache.get_instrument_metadata("EPIC1")
        await cache.get_instrument_metadata("EPIC1", force_refresh=True)
        assert client._request.await_count == 2


class TestQuantiseSize:
    async def test_rounds_down_to_step(self) -> None:
        # minDealSize = 0.04 → 2 decimal places → step = 0.01
        client = _make_client()
        cache = IGMetadataCache(client)
        client.get_instrument_metadata = AsyncMock(
            return_value={"dealingRules": {"minDealSize": {"value": 0.04}}}
        )

        result = await cache.quantise_size("EPIC", 1.1499)
        assert result == pytest.approx(1.14)

    async def test_fallback_to_2dp_when_no_dealing_rules(self) -> None:
        client = _make_client()
        cache = IGMetadataCache(client)
        client.get_instrument_metadata = AsyncMock(
            return_value={"dealingRules": None}
        )

        result = await cache.quantise_size("EPIC", 1.23456)
        assert result == pytest.approx(1.23)

    async def test_minimum_size_enforced(self) -> None:
        client = _make_client()
        cache = IGMetadataCache(client)
        client.get_instrument_metadata = AsyncMock(
            return_value={"dealingRules": {"minDealSize": {"value": 0.5}}}
        )

        result = await cache.quantise_size("EPIC", 0.1)
        assert result == pytest.approx(0.5)
