# tests/test_runner.py
"""
Tests for the runner module.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from tradedesk.portfolio.base import BasePortfolio
from tradedesk.runner import configure_logging, run_portfolio


class MinimalPortfolio(BasePortfolio):
    async def on_candle_close(self, event):
        pass


class TestConfigureLogging:
    def test_configure_logging_defaults(self):
        import logging

        root_logger = logging.getLogger()
        root_logger.handlers.clear()

        configure_logging("DEBUG")

        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0], logging.StreamHandler)

    def test_configure_logging_respects_existing(self):
        import logging

        root_logger = logging.getLogger()
        root_logger.handlers.clear()

        dummy_handler = logging.NullHandler()
        root_logger.addHandler(dummy_handler)
        root_logger.setLevel(logging.WARNING)

        configure_logging("DEBUG")

        assert root_logger.level == logging.WARNING
        assert len(root_logger.handlers) == 1
        assert root_logger.handlers[0] == dummy_handler

    def test_configure_logging_force(self):
        import logging

        root_logger = logging.getLogger()
        root_logger.handlers.clear()

        root_logger.addHandler(logging.NullHandler())

        configure_logging("DEBUG", force=True)

        assert root_logger.level == logging.DEBUG
        assert len(root_logger.handlers) == 1
        assert isinstance(root_logger.handlers[0], logging.StreamHandler)


class TestRunPortfolio:
    def test_run_portfolio_calls_portfolio_run(self):
        """Portfolio's run() is awaited."""
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.close = AsyncMock()

        mock_portfolio = MagicMock()
        mock_portfolio.run = AsyncMock()

        run_portfolio(
            portfolio_factory=lambda c: mock_portfolio,
            client_factory=lambda: mock_client,
            setup_logging=False,
        )

        mock_client.start.assert_awaited_once()
        mock_portfolio.run.assert_awaited_once()
        mock_client.close.assert_awaited_once()

    def test_run_portfolio_client_error_exits(self):
        """Runner exits with code 1 if portfolio.run() raises."""
        mock_client = MagicMock()
        mock_client.start = AsyncMock()
        mock_client.close = AsyncMock()

        mock_portfolio = MagicMock()
        mock_portfolio.run = AsyncMock(side_effect=Exception("broker error"))

        with patch("sys.exit") as mock_exit:
            run_portfolio(
                portfolio_factory=lambda c: mock_portfolio,
                client_factory=lambda: mock_client,
                setup_logging=False,
            )

            mock_exit.assert_called_with(1)
            mock_client.close.assert_awaited_once()

    def test_run_portfolio_keyboard_interrupt(self):
        """Graceful handling of KeyboardInterrupt."""
        with patch("asyncio.run", side_effect=KeyboardInterrupt()):
            with patch("logging.Logger.info") as mock_info:
                run_portfolio(
                    portfolio_factory=lambda c: MagicMock(),
                    client_factory=lambda: MagicMock(),
                    setup_logging=False,
                )

                mock_info.assert_any_call("Interrupted by user - shutting down gracefully")
