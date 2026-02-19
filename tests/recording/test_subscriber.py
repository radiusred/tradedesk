import asyncio

from tradedesk.events import get_dispatcher, reset_dispatcher
from tradedesk.execution.events import OrderCompletedEvent, OrderRequestEvent
from tradedesk.recording.ledger import TradeLedger
from tradedesk.recording.subscriber import register_recording_subscriber
from tradedesk.types import OrderRequest, OrderResult


def test_order_completed_updates_ledger() -> None:
    reset_dispatcher()

    ledger = TradeLedger()
    # Register subscriber which will write into our ledger
    register_recording_subscriber(ledger=ledger)

    request = OrderRequest(instrument="EURUSD", direction="BUY", size=1.0)
    request_id = "test-req-1"

    # Publish request then a completed event
    asyncio.run(get_dispatcher().publish(OrderRequestEvent(request=request, request_id=request_id)))

    result = OrderResult(success=True, fill_price=1.2345, fill_size=1.0, raw={}, error="")
    asyncio.run(get_dispatcher().publish(OrderCompletedEvent(request_id=request_id, result=result)))

    assert len(ledger.trades) == 1
    tr = ledger.trades[0]
    assert tr.instrument == "EURUSD"
    assert tr.direction == "BUY"
    assert tr.size == 1.0
    assert tr.price == 1.2345
