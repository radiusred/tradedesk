"""
Strategy orchestration and execution.
"""

import asyncio
import logging
import sys
from collections.abc import Callable
from typing import Any

from tradedesk.execution import Client, OrderExecutionHandler
from tradedesk.strategy import BaseStrategy

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


def _instruments_from_subscriptions(strategy: BaseStrategy) -> list[str]:
    instruments: list[str] = []
    seen = set()

    for sub in getattr(strategy, "subscriptions", []) or []:
        instrument = getattr(sub, "instrument", None)
        if instrument and instrument not in seen:
            seen.add(instrument)
            instruments.append(instrument)

    return instruments


async def _run_strategies_async(
    strategy_instances: list[BaseStrategy], client: Client
) -> None:
    """
    Run multiple strategies concurrently.

    Notes:
    - Task cancellation/cleanup is handled in a finally block so it runs on:
      * normal completion
      * CancelledError
      * KeyboardInterrupt propagating through asyncio.run(main())
      * any other exception
    """
    if not strategy_instances:
        log.warning("No strategies to run")
        return

    for strategy in strategy_instances:
        instruments = _instruments_from_subscriptions(strategy)
        log.info(
            "Loaded %s monitoring %d instrument%s: %s",
            strategy.__class__.__name__,
            len(instruments),
            "s" if len(instruments) != 1 else "",
            ", ".join(instruments) if instruments else "(none)",
        )

    all_instruments = set()
    for strategy in strategy_instances:
        all_instruments.update(_instruments_from_subscriptions(strategy))

    if all_instruments:
        log.info("Total unique instruments to monitor: %d", len(all_instruments))
    else:
        log.warning("No instruments defined in any strategy - nothing to monitor")

    tasks = [asyncio.create_task(strategy.run()) for strategy in strategy_instances]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        # Ensure a friendly log line when cancellation is the reason.
        log.info("Strategies cancelled")
        raise
    finally:
        # Always attempt to stop strategies cleanly.
        for task in tasks:
            if not task.done():
                task.cancel()

        # Drain tasks; swallow exceptions to avoid masking the original failure/cancel.
        await asyncio.gather(*tasks, return_exceptions=True)


def _instantiate_strategies(
    client: Client, strategy_specs: list[Any]
) -> list[BaseStrategy]:
    """
    Instantiate strategies.

    strategy_specs entries can be:
      - a BaseStrategy subclass (old format)
      - a tuple of (StrategyClass, kwargs_dict) (new format)
    """
    strategy_instances: list[BaseStrategy] = []

    for spec in strategy_specs:
        if isinstance(spec, tuple):
            strategy_class, kwargs = spec
            log.debug("Strategy %s kwargs=%s", strategy_class.__name__, kwargs)
            instance = strategy_class(client, **kwargs)
        else:
            strategy_class = spec
            instance = strategy_class(client)

        strategy_instances.append(instance)

    return strategy_instances


async def _async_run_strategies(
    client: Client,
    strategy_specs: list[Any],
    log_level: str | None = None,
    setup_logging: bool = True,
) -> None:
    if setup_logging:
        configure_logging(log_level or "INFO")

    log.info("=" * 70)
    log.info("Tradedesk Strategy Runner")
    log.info("=" * 70)

    try:
        # Wire up event-driven order execution before strategies start.
        _order_handler = OrderExecutionHandler(client)  # noqa: F841

        strategy_instances = _instantiate_strategies(client, strategy_specs)

        log.info("Starting strategies...")
        log.info("-" * 70)

        await _run_strategies_async(strategy_instances, client)

    finally:
        # Always close the client even if strategy instantiation fails.
        await client.close()


async def _async_run_with_client_factory(
    client_factory: Callable[[], Client],
    strategy_specs: list[Any],
    log_level: str | None = None,
    setup_logging: bool = True,
) -> None:
    client = client_factory()
    await client.start()
    await _async_run_strategies(
        client,
        strategy_specs,
        log_level=log_level,
        setup_logging=setup_logging,
    )


def run_strategies(
    strategy_specs: list[Any],
    client_factory: Callable[[], Client],
    log_level: str | None = None,
    setup_logging: bool = True,
) -> None:
    """
    Run one or more strategies against a live data provider.

    This is the main synchronous entry point for the `tradedesk` runner.
    It manages the asyncio event loop and the lifecycle of the provider
    client and strategies.

    The framework will:
      - Construct the provider client via `client_factory()`.
      - Await `client.start()` to authenticate and connect.
      - Instantiate and run all strategies concurrently.
      - Await `client.close()` on graceful shutdown or error.

    Args:
        strategy_specs: A list of strategy specifications. Each spec can be
            a `BaseStrategy` subclass, or a tuple of `(StrategyClass, kwargs)`.
        client_factory: A callable that returns an unstarted `Client` instance.
        log_level: The logging level to configure (e.g., "INFO", "DEBUG").
            If `None`, defaults to "INFO".
        setup_logging: If `True`, configures the root logger. Set to `False`
            if the application handles its own logging setup.
    """
    exit_code = 0

    try:
        asyncio.run(
            _async_run_with_client_factory(
                client_factory=client_factory,
                strategy_specs=strategy_specs,
                log_level=log_level,
                setup_logging=setup_logging,
            )
        )

    except KeyboardInterrupt:
        log.info("")
        log.info("-" * 70)
        log.info("Interrupted by user - shutting down gracefully")

    except Exception as e:
        log.exception("Fatal error in strategy runner: %s", e)
        exit_code = 1

    finally:
        log.info("=" * 70)
        log.info("Tradedesk shut down complete")
        log.info("=" * 70)

    if exit_code:
        sys.exit(exit_code)
