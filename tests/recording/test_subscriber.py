import asyncio
from datetime import datetime, timezone

from tradedesk.events import get_dispatcher, reset_dispatcher
from tradedesk.recording.events import PositionClosedEvent, PositionOpenedEvent
from tradedesk.recording.ledger import TradeLedger
from tradedesk.recording.subscriber import register_recording_subscriber


def test_position_closed_records_entry_and_exit_trades() -> None:
    """Test that position events create trade records for entry and exit."""
    reset_dispatcher()

    ledger = TradeLedger()
    # Register subscriber which will write into our ledger
    register_recording_subscriber(ledger=ledger)

    # Publish position opened event
    entry_ts = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
    asyncio.run(
        get_dispatcher().publish(
            PositionOpenedEvent(
                instrument="EURUSD",
                direction="BUY",
                size=1.0,
                entry_price=1.2000,
                timestamp=entry_ts,
            )
        )
    )

    # Publish position closed event
    exit_ts = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    asyncio.run(
        get_dispatcher().publish(
            PositionClosedEvent(
                instrument="EURUSD",
                direction="BUY",  # Position was BUY
                size=1.0,
                entry_price=1.2000,
                exit_price=1.2100,
                pnl=10.0,
                exit_reason="take_profit",
                timestamp=exit_ts,
            )
        )
    )

    # Should have created 2 trades: entry and exit
    assert len(ledger.trades) == 2

    entry_trade = ledger.trades[0]
    assert entry_trade.instrument == "EURUSD"
    assert entry_trade.direction == "BUY"
    assert entry_trade.size == 1.0
    assert entry_trade.price == 1.2000
    assert entry_trade.reason == "entry"

    exit_trade = ledger.trades[1]
    assert exit_trade.instrument == "EURUSD"
    assert exit_trade.direction == "SELL"  # Opposite of position direction
    assert exit_trade.size == 1.0
    assert exit_trade.price == 1.2100
    assert exit_trade.reason == "take_profit"


def test_position_closed_without_opened_records_exit_only() -> None:
    """Test that position closed without open event still records the exit."""
    reset_dispatcher()

    ledger = TradeLedger()
    register_recording_subscriber(ledger=ledger)

    # Publish position closed WITHOUT a preceding opened event
    exit_ts = datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
    asyncio.run(
        get_dispatcher().publish(
            PositionClosedEvent(
                instrument="EURUSD",
                direction="SELL",
                size=1.0,
                entry_price=1.2000,
                exit_price=1.1900,
                pnl=10.0,
                exit_reason="stop_loss",
                timestamp=exit_ts,
            )
        )
    )

    # Should have only the exit trade
    assert len(ledger.trades) == 1
    exit_trade = ledger.trades[0]
    assert exit_trade.instrument == "EURUSD"
    assert exit_trade.direction == "BUY"  # Opposite of SELL position
    assert exit_trade.price == 1.1900
