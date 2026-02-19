"""Tests for tradedesk.execution.backtest.runner."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tradedesk.execution.backtest.runner import BacktestSpec, run_backtest
from tradedesk.types import Candle


@pytest.fixture
def mock_client_cls():
    with patch("tradedesk.execution.backtest.runner.BacktestClient") as mock:
        yield mock


@pytest.fixture
def mock_register_subscriber():
    with patch("tradedesk.execution.backtest.runner.register_recording_subscriber") as mock:
        yield mock


@pytest.fixture
def mock_compute_metrics():
    with patch("tradedesk.execution.backtest.runner.compute_metrics") as mock:
        # Return a dummy metrics object
        mock.return_value = MagicMock(
            trades=10,
            round_trips=5,
            final_equity=10500.0,
            max_drawdown=-100.0,
            win_rate=0.6,
            avg_win=50.0,
            avg_loss=-20.0,
            profit_factor=1.5,
            expectancy=10.0,
            avg_hold_minutes=15.0,
        )
        yield mock


@pytest.fixture
def mock_dispatcher():
    with patch("tradedesk.execution.backtest.runner.get_dispatcher") as mock:
        dispatcher = MagicMock()
        dispatcher.publish = AsyncMock()
        mock.return_value = dispatcher
        yield mock


@pytest.mark.asyncio
async def test_run_backtest_spread_adjustment(
    mock_client_cls, mock_register_subscriber, mock_compute_metrics, mock_dispatcher, tmp_path
):
    """Test that half_spread_adjustment modifies candle OHLC."""
    # Setup mock client instance
    client_instance = mock_client_cls.from_csv.return_value
    client_instance.start = AsyncMock()

    # Setup a candle series
    candle = MagicMock(spec=Candle)
    candle.open = 100.0
    candle.high = 105.0
    candle.low = 95.0
    candle.close = 102.0

    series = MagicMock()
    series.candles = [candle]
    series.instrument = "TEST"
    series.period = "1MIN"

    streamer = MagicMock()
    streamer._candle_series = [series]
    streamer.run = AsyncMock()

    client_instance.get_streamer.return_value = streamer

    spec = BacktestSpec(
        instrument="TEST",
        period="1MIN",
        candle_csv=Path("dummy.csv"),
        half_spread_adjustment=0.5,
    )

    # Dummy strategy
    strat = MagicMock()
    strat._handle_event = AsyncMock()

    # Mock recorders to avoid instantiation issues
    with patch("tradedesk.execution.backtest.runner.EquityRecorder"), \
         patch("tradedesk.execution.backtest.runner.ProgressLogger"), \
         patch("tradedesk.execution.backtest.runner.build_candle_index"), \
         patch("tradedesk.execution.backtest.runner.ExcursionComputer"), \
         patch("tradedesk.execution.backtest.runner.TradeLedger"), \
         patch("tradedesk.execution.order_handler.OrderExecutionHandler"):

        await run_backtest(spec=spec, out_dir=tmp_path, strategy_factory=lambda c: strat)

    # Verify adjustment
    assert candle.open == 100.5
    assert candle.high == 105.5
    assert candle.low == 95.5
    assert candle.close == 102.5


@pytest.mark.asyncio
async def test_run_backtest_event_driven_recording(
    mock_client_cls, mock_register_subscriber, mock_compute_metrics, mock_dispatcher, tmp_path
):
    """Test that event-driven recording is set up correctly."""
    client_instance = mock_client_cls.from_csv.return_value
    client_instance.start = AsyncMock()
    streamer = MagicMock()
    streamer._candle_series = []
    streamer.run = AsyncMock()
    client_instance.get_streamer.return_value = streamer

    spec = BacktestSpec(
        instrument="TEST", period="1MIN", candle_csv=Path("dummy.csv")
    )

    # Mock all recorder classes and dependencies
    with patch("tradedesk.execution.backtest.runner.EquityRecorder") as mock_equity_recorder, \
         patch("tradedesk.execution.backtest.runner.ProgressLogger"), \
         patch("tradedesk.execution.backtest.runner.build_candle_index"), \
         patch("tradedesk.execution.backtest.runner.ExcursionComputer") as mock_excursion, \
         patch("tradedesk.execution.backtest.runner.TradeLedger"), \
         patch("tradedesk.execution.order_handler.OrderExecutionHandler"):

        await run_backtest(
            spec=spec, out_dir=tmp_path, strategy_factory=lambda c: MagicMock()
        )

        # Verify recorders were instantiated
        mock_equity_recorder.assert_called_once()
        mock_excursion.assert_called_once()

        # Verify session events were published
        dispatcher_instance = mock_dispatcher.return_value
        assert dispatcher_instance.publish.call_count >= 2  # SessionStarted and SessionEnded


@pytest.mark.asyncio
async def test_run_backtest_metrics_output(
    mock_client_cls, mock_register_subscriber, mock_compute_metrics, mock_dispatcher, tmp_path
):
    """Test that metrics are computed and returned as Metrics object."""
    client_instance = mock_client_cls.from_csv.return_value
    client_instance.start = AsyncMock()
    streamer = MagicMock()
    streamer._candle_series = []
    streamer.run = AsyncMock()
    client_instance.get_streamer.return_value = streamer

    spec = BacktestSpec(instrument="TEST", period="1MIN", candle_csv=Path("dummy.csv"))

    # Mock all dependencies
    with patch("tradedesk.execution.backtest.runner.EquityRecorder"), \
         patch("tradedesk.execution.backtest.runner.ProgressLogger"), \
         patch("tradedesk.execution.backtest.runner.build_candle_index"), \
         patch("tradedesk.execution.backtest.runner.ExcursionComputer"), \
         patch("tradedesk.execution.backtest.runner.TradeLedger"), \
         patch("tradedesk.execution.order_handler.OrderExecutionHandler"):

        result = await run_backtest(
            spec=spec, out_dir=tmp_path, strategy_factory=lambda c: MagicMock()
        )

    # Verify result is a Metrics object with expected attributes
    assert result.trades == 10
    assert result.round_trips == 5
    assert result.final_equity == 10500.0
    assert result.win_rate == 0.6
    assert result.avg_hold_minutes == 15.0
