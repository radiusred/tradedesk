# tradedesk/runner.py
"""
Strategy orchestration and execution.

Provides the run_strategies() function which:
- Sets up logging
- Validates configuration
- Creates an authenticated IG client
- Instantiates and runs user strategies
- Handles graceful shutdown
"""

import asyncio
import logging
import sys
from typing import List, Type

from .client import IGClient
from .config import settings
from .strategy import BaseStrategy

log = logging.getLogger(__name__)


def configure_logging(level: str = "INFO") -> None:
    """
    Configure root logger with console output.
    
    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    logger = logging.getLogger()
    logger.setLevel(level.upper())
    
    # Remove any existing handlers
    logger.handlers.clear()
    
    # Create console handler with formatting
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger.addHandler(handler)


async def _run_strategies_async(
    client: IGClient,
    strategy_instances: List[BaseStrategy]
) -> None:
    """
    Run multiple strategies concurrently.
    
    Args:
        client: Authenticated IG client
        strategy_instances: List of instantiated strategy objects
    """
    if not strategy_instances:
        log.warning("No strategies to run")
        return
    
    # Log what we're running
    for strategy in strategy_instances:
        log.info(
            "Loaded %s monitoring %d EPIC%s: %s",
            strategy.__class__.__name__,
            len(strategy.epics),
            "s" if len(strategy.epics) != 1 else "",
            ", ".join(strategy.epics) if strategy.epics else "(none)"
        )
    
    # Collect all unique EPICs across all strategies
    all_epics = set()
    for strategy in strategy_instances:
        all_epics.update(strategy.epics)
    
    if all_epics:
        log.info("Total unique EPICs to monitor: %d", len(all_epics))
    else:
        log.warning("No EPICs defined in any strategy - nothing to monitor")
    
    # Run all strategies concurrently
    tasks = [asyncio.create_task(strategy.run()) for strategy in strategy_instances]
    
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("Strategies cancelled")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def run_strategies(
    strategy_classes: List[Type[BaseStrategy]],
    log_level: str | None = None
) -> None:
    """
    Main entry point for running trading strategies.
    
    This function:
    1. Configures logging
    2. Validates configuration from .env
    3. Creates an authenticated IG client
    4. Instantiates all provided strategy classes
    5. Runs them concurrently until interrupted
    
    Args:
        strategy_classes: List of BaseStrategy subclasses to run
        log_level: Optional log level override (uses settings.log_level if None)
    
    Example:
        from tradedesk import run_strategies
        from my_strategies import MyStrategy, AnotherStrategy
        
        if __name__ == "__main__":
            run_strategies([MyStrategy, AnotherStrategy])
    
    Raises:
        ValueError: If configuration is invalid
        RuntimeError: If IG authentication fails
    """
    # Configure logging
    level = log_level or settings.log_level
    configure_logging(level)
    
    log.info("=" * 70)
    log.info("Tradedesk Strategy Runner")
    log.info("=" * 70)
    log.info("Environment: %s", settings.environment)
    log.info("Log level: %s", level)
    
    # Validate configuration
    try:
        settings.validate()
    except ValueError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)
    
    # Create IG client
    try:
        log.info("Authenticating with IG...")
        client = IGClient()
    except Exception as e:
        log.error("Failed to create IG client: %s", e)
        sys.exit(1)
    
    # Instantiate strategies
    try:
        strategy_instances = [cls(client) for cls in strategy_classes]
    except Exception as e:
        log.exception("Failed to instantiate strategies: %s", e)
        sys.exit(1)
    
    # Run strategies
    log.info("Starting strategies...")
    log.info("-" * 70)
    
    try:
        asyncio.run(_run_strategies_async(client, strategy_instances))
    except KeyboardInterrupt:
        log.info("")
        log.info("-" * 70)
        log.info("Interrupted by user - shutting down gracefully")
    except Exception as e:
        log.exception("Fatal error in strategy runner: %s", e)
        sys.exit(1)
    finally:
        log.info("=" * 70)
        log.info("Tradedesk shut down complete")
        log.info("=" * 70)
