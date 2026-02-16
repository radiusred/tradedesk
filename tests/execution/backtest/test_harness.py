"""Tests for tradedesk.execution.backtest.harness."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from tradedesk.execution.backtest.harness import BacktestSpec, run_backtest
from tradedesk.types import Candle


@pytest.fixture
def mock_client_cls():
    with patch("tradedesk.execution.backtest.harness.BacktestClient") as mock:
        yield mock


@pytest.fixture
def mock_ledger_cls():
    with patch("tradedesk.execution.backtest.harness.TradeLedger") as mock:
        yield mock


@pytest.fixture
def mock_compute_metrics():
    with patch("tradedesk.execution.backtest.harness.compute_metrics") as mock:
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


@pytest.mark.asyncio
async def test_run_backtest_spread_adjustment(
    mock_client_cls, mock_ledger_cls, mock_compute_metrics, tmp_path
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

    await run_backtest(spec=spec, out_dir=tmp_path, strategy_factory=lambda c: strat)

    # Verify adjustment
    assert candle.open == 100.5
    assert candle.high == 105.5
    assert candle.low == 95.5
    assert candle.close == 102.5


@pytest.mark.asyncio
async def test_run_backtest_equity_recording(
    mock_client_cls, mock_ledger_cls, mock_compute_metrics, tmp_path
):
    """Test that strategy event handler is wrapped to record equity."""
    client_instance = mock_client_cls.from_csv.return_value
    client_instance.start = AsyncMock()
    streamer = MagicMock()
    client_instance.get_streamer.return_value = streamer

    # Mock compute_equity
    with patch("tradedesk.execution.backtest.harness.compute_equity") as mock_eq:
        mock_eq.side_effect = [10000.0, 10100.0]

        # Strategy with _handle_event
        strat = MagicMock()
        original_handle = AsyncMock()
        strat._handle_event = original_handle

        # Simulate streamer running and calling the strategy
        async def simulate_stream(strategy):
            # The strategy passed here has the wrapped handler
            await strategy._handle_event(MagicMock(timestamp="2023-01-01T00:00:00Z"))
            await strategy._handle_event(MagicMock(timestamp="2023-01-01T00:01:00Z"))

        streamer.run.side_effect = simulate_stream

        spec = BacktestSpec(
            instrument="TEST", period="1MIN", candle_csv=Path("dummy.csv")
        )

        await run_backtest(
            spec=spec, out_dir=tmp_path, strategy_factory=lambda c: strat
        )

        # Verify original handle called
        assert original_handle.call_count == 2

        # Verify ledger recorded equity
        ledger_instance = mock_ledger_cls.return_value
        assert ledger_instance.record_equity.call_count == 2

        # Verify ledger write called
        ledger_instance.write.assert_called_with(tmp_path)


@pytest.mark.asyncio
async def test_run_backtest_metrics_output(
    mock_client_cls, mock_ledger_cls, mock_compute_metrics, tmp_path
):
    """Test that metrics are computed and returned in correct format."""
    client_instance = mock_client_cls.from_csv.return_value
    client_instance.start = AsyncMock()
    client_instance.get_streamer.return_value.run = AsyncMock()

    spec = BacktestSpec(
        instrument="TEST", period="1MIN", candle_csv=Path("dummy.csv")
    )

    result = await run_backtest(
        spec=spec, out_dir=tmp_path, strategy_factory=lambda c: MagicMock()
    )

    assert result["instrument"] == "TEST"
    assert result["final_equity"] == "10500.00"
    assert result["win_rate"] == "60.0"
    assert result["avg_hold_min"] == "15.0"


@pytest.mark.asyncio
async def test_run_backtest_ledger_fallback(
    mock_client_cls, mock_ledger_cls, mock_compute_metrics, tmp_path
):
    """Test fallback to legacy CSV writing if ledger.write() is missing."""
    client_instance = mock_client_cls.from_csv.return_value
    client_instance.start = AsyncMock()
    client_instance.get_streamer.return_value.run = AsyncMock()

    # Mock ledger without write method
    ledger_instance = mock_ledger_cls.return_value
    del ledger_instance.write

    spec = BacktestSpec(
        instrument="TEST", period="1MIN", candle_csv=Path("dummy.csv")
    )

    await run_backtest(
        spec=spec, out_dir=tmp_path, strategy_factory=lambda c: MagicMock()
    )

    ledger_instance.write_trades_csv.assert_called_with(tmp_path / "trades.csv")
    ledger_instance.write_equity_csv.assert_called_with(tmp_path / "equity.csv")
