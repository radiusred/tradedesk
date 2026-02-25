"""
Portfolio orchestration and execution.
"""

import asyncio
import logging
import sys
from collections.abc import Callable

from tradedesk.execution import Client
from tradedesk.portfolio.base import BasePortfolio

log = logging.getLogger(__name__)


def configure_logging(level: str = "INFO", force: bool = False) -> None:
    """
    Configure root logger with console output.

    By default, this is non-destructive: if the root logger already has handlers,
    it will do nothing (assuming the application has configured logging).

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
        force: If True, clear existing handlers and force this configuration
    """
    root_logger = logging.getLogger()

    if root_logger.hasHandlers() and not force:
        return

    root_logger.setLevel(level.upper())

    if force:
        root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


async def _async_run_portfolio(
    portfolio_factory: Callable[[Client], BasePortfolio],
    client_factory: Callable[[], Client],
    log_level: str | None,
    setup_logging: bool,
) -> None:
    if setup_logging:
        configure_logging(log_level or "INFO")

    log.info("=" * 70)
    log.info("Tradedesk Portfolio Runner")
    log.info("=" * 70)

    client = client_factory()
    await client.start()
    try:
        portfolio = portfolio_factory(client)
        await portfolio.run()
    finally:
        await client.close()
        log.info("=" * 70)
        log.info("Tradedesk shut down complete")
        log.info("=" * 70)


def run_portfolio(
    portfolio_factory: Callable[[Client], BasePortfolio],
    client_factory: Callable[[], Client],
    log_level: str | None = None,
    setup_logging: bool = True,
) -> None:
    """
    Run a portfolio against a live data provider.

    This is the main synchronous entry point for tradedesk. It manages the
    asyncio event loop and the full lifecycle of the client and portfolio.

    The framework will:
      - Construct the provider client via ``client_factory()``.
      - Await ``client.start()`` to authenticate and connect.
      - Construct the portfolio via ``portfolio_factory(client)``.
      - Await ``portfolio.run()`` which fires lifecycle events and streams.
      - Await ``client.close()`` on shutdown or error.

    Args:
        portfolio_factory: Callable that receives the started ``Client`` and
            returns an initialised (but not yet running) ``BasePortfolio``.
        client_factory: Callable that returns an unstarted ``Client`` instance.
        log_level: Logging level (e.g. ``"INFO"``, ``"DEBUG"``). Defaults to
            ``"INFO"``.
        setup_logging: If ``True``, configures the root logger. Set to
            ``False`` if the application handles its own logging setup.
    """
    exit_code = 0

    try:
        asyncio.run(
            _async_run_portfolio(
                portfolio_factory=portfolio_factory,
                client_factory=client_factory,
                log_level=log_level,
                setup_logging=setup_logging,
            )
        )

    except KeyboardInterrupt:
        log.info("")
        log.info("-" * 70)
        log.info("Interrupted by user - shutting down gracefully")

    except Exception as e:
        if log.getEffectiveLevel() <= logging.DEBUG:
            log.exception("Fatal error in portfolio runner", exc_info=e)
        else:
            log.error("Fatal error in portfolio runner: %s", e)
            log.error("Run with DEBUG logging for more details")
        exit_code = 1

    if exit_code:
        sys.exit(exit_code)
