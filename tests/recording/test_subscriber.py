import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from tradedesk.events import (
    SessionEndedEvent,
    SessionStartedEvent,
    get_dispatcher,
    reset_dispatcher,
)
from tradedesk.recording.events import (
    PositionClosedEvent,
    PositionOpenedEvent,
    ReportingCompleteEvent,
)
from tradedesk.recording.ledger import TradeLedger
from tradedesk.recording.subscriber import RecordingSubscriber, register_recording_subscriber
from tradedesk.recording.types import EquityRecord, TradeRecord


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


# ---------------------------------------------------------------------------
# handle_session_started
# ---------------------------------------------------------------------------


def test_session_started_creates_timestamped_output_dir(tmp_path: Path) -> None:
    """SessionStartedEvent creates a timestamped subdirectory under the base output dir."""
    ts = datetime(2024, 6, 15, 9, 30, 0, tzinfo=timezone.utc)
    sub = RecordingSubscriber(output_dir=tmp_path)

    sub.handle_session_started(SessionStartedEvent(timestamp=ts))

    expected = tmp_path / "20240615_093000"
    assert expected.is_dir()
    assert sub._run_output_dir == expected


def test_session_started_no_output_dir_is_noop() -> None:
    """When no output_dir is configured, session start does nothing."""
    sub = RecordingSubscriber()

    sub.handle_session_started(SessionStartedEvent())

    assert sub._run_output_dir is None


def test_session_started_via_dispatcher(tmp_path: Path) -> None:
    """SessionStartedEvent dispatched through the event bus reaches the subscriber."""
    reset_dispatcher()
    ts = datetime(2024, 3, 1, 12, 0, 0, tzinfo=timezone.utc)

    register_recording_subscriber(output_dir=tmp_path)

    asyncio.run(get_dispatcher().publish(SessionStartedEvent(timestamp=ts)))

    assert (tmp_path / "20240301_120000").is_dir()


# ---------------------------------------------------------------------------
# handle_session_ended
# ---------------------------------------------------------------------------


def _make_subscriber_with_trades(
    tmp_path: Path,
) -> RecordingSubscriber:
    """Helper: create a subscriber that has an active run dir and some trades."""
    sub = RecordingSubscriber(output_dir=tmp_path)
    ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub.handle_session_started(SessionStartedEvent(timestamp=ts))

    sub.ledger.record_equity(EquityRecord(timestamp="2024-01-01T00:00:00+00:00", equity=0.0))
    sub.ledger.record_trade(
        TradeRecord(
            timestamp="2024-01-01T00:00:00+00:00",
            instrument="EURUSD",
            direction="BUY",
            size=1.0,
            price=1.20,
        )
    )
    sub.ledger.record_trade(
        TradeRecord(
            timestamp="2024-01-01T01:00:00+00:00",
            instrument="EURUSD",
            direction="SELL",
            size=1.0,
            price=1.21,
        )
    )
    sub.ledger.record_equity(EquityRecord(timestamp="2024-01-01T01:00:00+00:00", equity=10.0))
    return sub


@pytest.mark.asyncio
async def test_session_ended_writes_ledger_files(tmp_path: Path) -> None:
    """SessionEndedEvent writes all expected CSV files to the run output directory."""
    sub = _make_subscriber_with_trades(tmp_path)
    run_dir = sub._run_output_dir
    assert run_dir is not None

    with patch("tradedesk.recording.subscriber.get_dispatcher") as mock_disp:
        mock_disp.return_value.publish = _noop_publish
        await sub.handle_session_ended(SessionEndedEvent())

    assert (run_dir / "trades.csv").exists()
    assert (run_dir / "round_trips.csv").exists()
    assert (run_dir / "metrics.csv").exists()
    assert (run_dir / "equity.csv").exists()


async def _noop_publish(event: object) -> None:
    """Stub for dispatcher.publish so ReportingCompleteEvent doesn't propagate."""
    pass


@pytest.mark.asyncio
async def test_session_ended_emits_reporting_complete_event(tmp_path: Path) -> None:
    """SessionEndedEvent always emits a ReportingCompleteEvent at the end."""
    reset_dispatcher()
    sub = _make_subscriber_with_trades(tmp_path)

    received: list[ReportingCompleteEvent] = []

    async def capture(evt: ReportingCompleteEvent) -> None:
        received.append(evt)

    get_dispatcher().subscribe(ReportingCompleteEvent, capture)

    await sub.handle_session_ended(SessionEndedEvent())

    assert len(received) == 1
    assert isinstance(received[0], ReportingCompleteEvent)


@pytest.mark.asyncio
async def test_session_ended_no_output_dir_still_logs_metrics(tmp_path: Path) -> None:
    """When no output_dir is set, metrics are computed but no files are written."""
    sub = RecordingSubscriber()  # no output_dir
    sub.ledger.record_equity(EquityRecord(timestamp="2024-01-01T00:00:00+00:00", equity=0.0))
    sub.ledger.record_trade(
        TradeRecord(
            timestamp="2024-01-01T00:00:00+00:00",
            instrument="EURUSD",
            direction="BUY",
            size=1.0,
            price=1.20,
        )
    )
    sub.ledger.record_trade(
        TradeRecord(
            timestamp="2024-01-01T01:00:00+00:00",
            instrument="EURUSD",
            direction="SELL",
            size=1.0,
            price=1.21,
        )
    )

    with patch("tradedesk.recording.subscriber.get_dispatcher") as mock_disp:
        mock_disp.return_value.publish = _noop_publish
        # Should not raise even though there's nowhere to write
        await sub.handle_session_ended(SessionEndedEvent())

    # No files created (no run dir)
    assert sub._run_output_dir is None


@pytest.mark.asyncio
async def test_session_ended_no_trades_skips_report(tmp_path: Path) -> None:
    """When no trades exist, the report generation step is skipped."""
    sub = RecordingSubscriber(output_dir=tmp_path)
    ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    sub.handle_session_started(SessionStartedEvent(timestamp=ts))

    with patch("tradedesk.recording.subscriber.get_dispatcher") as mock_disp:
        mock_disp.return_value.publish = _noop_publish
        await sub.handle_session_ended(SessionEndedEvent())

    run_dir = sub._run_output_dir
    assert run_dir is not None
    # analysis.md should not be created when there are no trades
    assert not (run_dir / "analysis.md").exists()


@pytest.mark.asyncio
async def test_session_ended_ledger_write_failure_does_not_prevent_completion(
    tmp_path: Path,
) -> None:
    """If ledger.write() raises, the handler still emits ReportingCompleteEvent."""
    reset_dispatcher()
    sub = _make_subscriber_with_trades(tmp_path)

    received: list[ReportingCompleteEvent] = []

    async def capture(evt: ReportingCompleteEvent) -> None:
        received.append(evt)

    get_dispatcher().subscribe(ReportingCompleteEvent, capture)

    with patch.object(sub.ledger, "write", side_effect=OSError("disk full")):
        await sub.handle_session_ended(SessionEndedEvent())

    # ReportingCompleteEvent still emitted despite the write failure
    assert len(received) == 1


@pytest.mark.asyncio
async def test_session_ended_via_dispatcher(tmp_path: Path) -> None:
    """Full integration: SessionEndedEvent via the event bus writes files."""
    reset_dispatcher()
    sub = register_recording_subscriber(output_dir=tmp_path)

    start_ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    await get_dispatcher().publish(SessionStartedEvent(timestamp=start_ts))

    sub.ledger.record_equity(EquityRecord(timestamp="2024-01-01T00:00:00+00:00", equity=0.0))
    sub.ledger.record_trade(
        TradeRecord(
            timestamp="2024-01-01T00:00:00+00:00",
            instrument="EURUSD",
            direction="BUY",
            size=1.0,
            price=1.20,
        )
    )
    sub.ledger.record_trade(
        TradeRecord(
            timestamp="2024-01-01T01:00:00+00:00",
            instrument="EURUSD",
            direction="SELL",
            size=1.0,
            price=1.21,
        )
    )
    sub.ledger.record_equity(EquityRecord(timestamp="2024-01-01T01:00:00+00:00", equity=10.0))

    await get_dispatcher().publish(SessionEndedEvent())

    run_dir = tmp_path / "20240101_000000"
    assert run_dir.is_dir()
    assert (run_dir / "trades.csv").exists()
    assert (run_dir / "metrics.csv").exists()
    assert (run_dir / "round_trips.csv").exists()
