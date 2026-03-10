"""Tests for tradedesk.execution.backtest.runner."""

from datetime import date
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


def _make_spec(cache_dir: Path = Path("/cache")) -> BacktestSpec:
    return BacktestSpec(
        instrument="TEST",
        period="1MIN",
        cache_dir=cache_dir,
        symbol="EURUSD",
        date_from=date(2025, 1, 1),
        date_to=date(2025, 1, 31),
    )


@pytest.mark.asyncio
async def test_run_backtest_spread_adjustment(
    mock_client_cls, mock_register_subscriber, mock_compute_metrics, tmp_path
):
    """Test that half_spread_adjustment modifies candle OHLC."""
    client_instance = mock_client_cls.from_dukascopy_cache.return_value
    client_instance.start = AsyncMock()

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
        cache_dir=tmp_path,
        symbol="EURUSD",
        date_from=date(2025, 1, 1),
        date_to=date(2025, 1, 31),
        half_spread_adjustment=0.5,
    )

    mock_portfolio = MagicMock()
    mock_portfolio.run = AsyncMock()

    with (
        patch("tradedesk.execution.backtest.runner.EquityRecorder"),
        patch("tradedesk.execution.backtest.runner.ProgressLogger"),
        patch("tradedesk.execution.backtest.runner.build_candle_index"),
        patch("tradedesk.execution.backtest.runner.ExcursionComputer"),
        patch("tradedesk.execution.backtest.runner.TradeLedger"),
    ):
        await run_backtest(spec=spec, out_dir=tmp_path, portfolio_factory=lambda c: mock_portfolio)

    assert candle.open == 100.5
    assert candle.high == 105.5
    assert candle.low == 95.5
    assert candle.close == 102.5


@pytest.mark.asyncio
async def test_run_backtest_event_driven_recording(
    mock_client_cls, mock_register_subscriber, mock_compute_metrics, tmp_path
):
    """Test that event-driven recording is set up correctly."""
    client_instance = mock_client_cls.from_dukascopy_cache.return_value
    client_instance.start = AsyncMock()
    streamer = MagicMock()
    streamer._candle_series = []
    streamer.run = AsyncMock()
    client_instance.get_streamer.return_value = streamer

    spec = _make_spec(tmp_path)

    mock_portfolio = MagicMock()
    mock_portfolio.run = AsyncMock()

    with (
        patch("tradedesk.execution.backtest.runner.EquityRecorder") as mock_equity_recorder,
        patch("tradedesk.execution.backtest.runner.ProgressLogger"),
        patch("tradedesk.execution.backtest.runner.build_candle_index"),
        patch("tradedesk.execution.backtest.runner.ExcursionComputer") as mock_excursion,
        patch("tradedesk.execution.backtest.runner.TradeLedger"),
    ):
        await run_backtest(spec=spec, out_dir=tmp_path, portfolio_factory=lambda c: mock_portfolio)

        mock_equity_recorder.assert_called_once()
        mock_excursion.assert_called_once()
        mock_portfolio.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_backtest_metrics_output(
    mock_client_cls, mock_register_subscriber, mock_compute_metrics, tmp_path
):
    """Test that metrics are computed and returned as Metrics object."""
    client_instance = mock_client_cls.from_dukascopy_cache.return_value
    client_instance.start = AsyncMock()
    streamer = MagicMock()
    streamer._candle_series = []
    streamer.run = AsyncMock()
    client_instance.get_streamer.return_value = streamer

    spec = _make_spec(tmp_path)

    mock_portfolio = MagicMock()
    mock_portfolio.run = AsyncMock()

    with (
        patch("tradedesk.execution.backtest.runner.EquityRecorder"),
        patch("tradedesk.execution.backtest.runner.ProgressLogger"),
        patch("tradedesk.execution.backtest.runner.build_candle_index"),
        patch("tradedesk.execution.backtest.runner.ExcursionComputer"),
        patch("tradedesk.execution.backtest.runner.TradeLedger"),
    ):
        result = await run_backtest(
            spec=spec, out_dir=tmp_path, portfolio_factory=lambda c: mock_portfolio
        )

    assert result.trades == 10
    assert result.round_trips == 5
    assert result.final_equity == 10500.0
    assert result.win_rate == 0.6
    assert result.avg_hold_minutes == 15.0


def test_backtest_spec_defaults():
    spec = BacktestSpec(
        instrument="GBPUSD",
        period="15MIN",
        cache_dir=Path("/data/cache"),
        symbol="GBPUSD",
        date_from=date(2025, 1, 1),
        date_to=date(2025, 12, 31),
    )
    assert spec.price_side == "bid"
    assert spec.size == 1.0
    assert spec.half_spread_adjustment == 0.0
    assert spec.reporting_scale == 1.0
