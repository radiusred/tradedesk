"""Unit tests for IGOrderHandler."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from tradedesk.execution.broker import DealRejectedException
from tradedesk.execution.ig.orders import IGOrderHandler


def _make_client(account_type: str = "CFD") -> MagicMock:
    client = MagicMock()
    client._request = AsyncMock()
    client._ensure_account_type = AsyncMock(return_value=account_type)
    return client


class TestPlaceMarketOrder:
    async def test_builds_correct_payload(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        result = await handler.place_market_order(
            instrument="CS.D.GBPUSD.TODAY.IP",
            direction="BUY",
            size=1.0,
        )
        assert result == {"dealReference": "REF1"}

        (_, url), kwargs = client._request.call_args
        assert url == "/positions/otc"
        body = kwargs["json"]
        assert body["epic"] == "CS.D.GBPUSD.TODAY.IP"
        assert body["direction"] == "BUY"
        assert body["size"] == 1.0
        assert body["currencyCode"] == "GBP"
        assert body["forceOpen"] is False
        assert body["guaranteedStop"] is False

    async def test_sets_dfb_expiry_for_spreadbet(self) -> None:
        client = _make_client("SPREADBET")
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "BUY", 1.0)

        body = client._request.call_args.kwargs["json"]
        assert body["expiry"] == "DFB"

    async def test_preserves_custom_expiry_on_spreadbet(self) -> None:
        client = _make_client("SPREADBET")
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "BUY", 1.0, expiry="JUN-26")

        body = client._request.call_args.kwargs["json"]
        assert body["expiry"] == "JUN-26"

    async def test_does_not_override_expiry_for_cfd(self) -> None:
        client = _make_client("CFD")
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "BUY", 1.0)

        body = client._request.call_args.kwargs["json"]
        assert body["expiry"] == "-"

    async def test_calls_client_ensure_account_type(self) -> None:
        """IGOrderHandler must call _ensure_account_type on the client,
        so tests that monkeypatch client._ensure_account_type work correctly."""
        client = _make_client("CFD")
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "BUY", 1.0)
        client._ensure_account_type.assert_awaited_once()


class TestConfirmDeal:
    async def test_polls_until_accepted(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=[
                {"dealStatus": "PENDING"},
                {"dealStatus": "ACCEPTED"},
            ]
        )
        handler = IGOrderHandler(client)

        result = await handler.confirm_deal("REF1", timeout_s=1.0, poll_s=0.0)
        assert result["dealStatus"] == "ACCEPTED"
        assert client._request.await_count == 2

    async def test_raises_timeout_if_always_pending(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealStatus": "PENDING"})
        handler = IGOrderHandler(client)

        with pytest.raises(TimeoutError):
            await handler.confirm_deal("REF1", timeout_s=0.01, poll_s=0.0)

    async def test_retries_404_deal_not_found(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=[
                RuntimeError(
                    "IG request failed: HTTP 404: error.confirms.deal-not-found"
                ),
                {"dealStatus": "ACCEPTED"},
            ]
        )
        handler = IGOrderHandler(client)

        result = await handler.confirm_deal("REF1", timeout_s=1.0, poll_s=0.0)
        assert result["dealStatus"] == "ACCEPTED"

    async def test_raises_non_retryable_error(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=RuntimeError("IG request failed: HTTP 400: bad request")
        )
        handler = IGOrderHandler(client)

        with pytest.raises(RuntimeError, match="HTTP 400"):
            await handler.confirm_deal("REF1", timeout_s=1.0, poll_s=0.0)


class TestPlaceMarketOrderConfirmed:
    async def test_places_and_confirms(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=[
                {"dealReference": "REF1"},  # place
                {"dealStatus": "ACCEPTED", "level": 1.25},  # confirm
            ]
        )
        handler = IGOrderHandler(client)

        result = await handler.place_market_order_confirmed("EPIC", "BUY", 1.0)
        assert result["dealStatus"] == "ACCEPTED"
        assert result["level"] == 1.25

    async def test_raises_if_no_deal_reference(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={})
        handler = IGOrderHandler(client)

        with pytest.raises(RuntimeError, match="dealReference"):
            await handler.place_market_order_confirmed("EPIC", "BUY", 1.0)

    async def test_raises_deal_rejected_exception(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=[
                {"dealReference": "REF1"},
                {"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"},
            ]
        )
        handler = IGOrderHandler(client)

        with pytest.raises(DealRejectedException):
            await handler.place_market_order_confirmed("EPIC", "BUY", 1.0)

    async def test_passes_confirm_timeout_and_poll(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=[
                {"dealReference": "REF1"},
                {"dealStatus": "ACCEPTED"},
            ]
        )
        handler = IGOrderHandler(client)
        handler.confirm_deal = AsyncMock(return_value={"dealStatus": "ACCEPTED"})

        await handler.place_market_order_confirmed(
            "EPIC", "BUY", 1.0, confirm_timeout_s=30.0, confirm_poll_s=0.5
        )

        handler.confirm_deal.assert_awaited_once_with(
            "REF1", timeout_s=30.0, poll_s=0.5
        )


class TestPlaceMarketOrderExtended:
    async def test_direction_uppercased(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "sell", 2.0)

        body = client._request.call_args.kwargs["json"]
        assert body["direction"] == "SELL"

    async def test_force_open_and_guaranteed_stop(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order(
            "EPIC", "BUY", 1.0, force_open=True, guaranteed_stop=True
        )

        body = client._request.call_args.kwargs["json"]
        assert body["forceOpen"] is True
        assert body["guaranteedStop"] is True

    async def test_spreadbet_empty_string_expiry_becomes_dfb(self) -> None:
        client = _make_client("SPREADBET")
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "BUY", 1.0, expiry="")

        body = client._request.call_args.kwargs["json"]
        assert body["expiry"] == "DFB"

    async def test_uses_api_version_1(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order("EPIC", "BUY", 1.0)

        args, kwargs = client._request.call_args
        assert args == ("POST", "/positions/otc")
        assert kwargs["api_version"] == "1"

    async def test_custom_currency_and_time_in_force(self) -> None:
        client = _make_client()
        client._request = AsyncMock(return_value={"dealReference": "REF1"})
        handler = IGOrderHandler(client)

        await handler.place_market_order(
            "EPIC", "BUY", 1.0, currency="USD", time_in_force="EXECUTE_AND_ELIMINATE"
        )

        body = client._request.call_args.kwargs["json"]
        assert body["currencyCode"] == "USD"
        assert body["timeInForce"] == "EXECUTE_AND_ELIMINATE"


class TestConfirmDealExtended:
    async def test_retries_500_transient_error(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=[
                RuntimeError("IG request failed: HTTP 500: server error"),
                {"dealStatus": "ACCEPTED"},
            ]
        )
        handler = IGOrderHandler(client)

        result = await handler.confirm_deal("REF1", timeout_s=1.0, poll_s=0.0)
        assert result["dealStatus"] == "ACCEPTED"
        assert client._request.await_count == 2

    async def test_timeout_includes_last_error(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            side_effect=RuntimeError(
                "IG request failed: HTTP 500: server error"
            )
        )
        handler = IGOrderHandler(client)

        with pytest.raises(TimeoutError, match="last error"):
            await handler.confirm_deal("REF1", timeout_s=0.01, poll_s=0.0)

    async def test_immediate_accepted_no_polling(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            return_value={"dealStatus": "ACCEPTED", "dealId": "D1"}
        )
        handler = IGOrderHandler(client)

        result = await handler.confirm_deal("REF1", timeout_s=1.0, poll_s=0.0)
        assert result["dealId"] == "D1"
        assert client._request.await_count == 1

    async def test_returns_rejected_status(self) -> None:
        client = _make_client()
        client._request = AsyncMock(
            return_value={"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"}
        )
        handler = IGOrderHandler(client)

        result = await handler.confirm_deal("REF1", timeout_s=1.0, poll_s=0.0)
        assert result["dealStatus"] == "REJECTED"
