"""Tests for tradedesk.recording.recorders – recording, progress, tracker sync."""

from unittest.mock import MagicMock, patch

from tradedesk.recording.ledger import TradeLedger
from tradedesk.recording.recorders import (
    BacktestRecorder,
    ProgressLogger,
    TrackerSync,
)
from tradedesk.recording.types import TradeRecord
from tradedesk.types import Candle


def _candle(ts="2025-01-15T12:00:00Z"):
    return Candle(timestamp=ts, open=100.0, high=101.0, low=99.0, close=100.5)


# ---------------------------------------------------------------------------
# BacktestRecorder
# ---------------------------------------------------------------------------


class TestBacktestRecorder:
    def test_sample_equity(self):
        ledger = TradeLedger()
        recorder = BacktestRecorder(ledger)

        mock_inner = MagicMock()
        mock_inner.positions = {}
        mock_inner.realised_pnl = 100.0
        mock_client = MagicMock()
        mock_client._inner = mock_inner

        with patch(
            "tradedesk.recording.recorders.compute_equity", return_value=100.0
        ):
            recorder.sample_equity(_candle(), mock_client)

        assert len(ledger.equity) == 1
        assert ledger.equity[0].equity == 100.0

    def test_sample_equity_no_inner(self):
        """If client has no _inner attribute, should skip gracefully."""
        ledger = TradeLedger()
        recorder = BacktestRecorder(ledger)

        mock_client = MagicMock(spec=[])  # no _inner
        recorder.sample_equity(_candle(), mock_client)
        assert len(ledger.equity) == 0


# ---------------------------------------------------------------------------
# ProgressLogger
# ---------------------------------------------------------------------------


class TestProgressLogger:
    def test_logs_at_start_of_week(self):
        logger = ProgressLogger()
        with patch("tradedesk.recording.recorders.log") as mock_log:
            logger.on_candle(_candle("2025-01-13T00:00:00Z"))  # Monday week 3
            assert mock_log.info.called

    def test_does_not_log_same_week_twice(self):
        logger = ProgressLogger()
        with patch("tradedesk.recording.recorders.log") as mock_log:
            logger.on_candle(_candle("2025-01-13T00:00:00Z"))
            logger.on_candle(_candle("2025-01-14T00:00:00Z"))  # Same week
            assert mock_log.info.call_count == 1

    def test_logs_new_week(self):
        logger = ProgressLogger()
        with patch("tradedesk.recording.recorders.log") as mock_log:
            logger.on_candle(_candle("2025-01-13T00:00:00Z"))
            logger.on_candle(_candle("2025-01-20T00:00:00Z"))  # Next week
            assert mock_log.info.call_count == 2


# ---------------------------------------------------------------------------
# TrackerSync
# ---------------------------------------------------------------------------

# TODO: TrackerSync is now event-driven and reacts to PositionClosedEvent.
# These tests are for the old polling implementation and need to be rewritten
# to test event-driven behavior. For now, TrackerSync is tested implicitly
# through integration tests.

# class TestTrackerSync:
#     def test_sync_no_tracker(self):
#         """If policy has no tracker attribute, sync is a noop."""
#         policy = MagicMock(spec=[])  # no tracker
#         ts = TrackerSync(policy)
#         # Event-driven, no manual sync() method
#
#     def test_sync_below_threshold(self):
#         """Old polling test - not applicable to event-driven implementation."""
#         pass
#
#     def test_sync_above_threshold_pushes_round_trips(self):
#         """Old polling test - not applicable to event-driven implementation."""
#         pass
