# tradedesk/runner.py
"""
Strategy orchestration and execution.
"""

import asyncio
import logging
import sys

from .client import IGClient
from .config import settings
from .strategy import BaseStrategy

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
    
    # If logging is already configured and we aren't forcing it, exit.
    if root_logger.hasHandlers() and not force:
        return

    root_logger.setLevel(level.upper())
    
    if force:
        root_logger.handlers.clear()
    
    # Create console handler with formatting
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger.addHandler(handler)


async def _run_strategies_async(
    strategy_instances: list[BaseStrategy],
    client: IGClient
) -> None:
    """
    Run multiple strategies concurrently.
    
    Args:
        strategy_instances: List of instantiated strategy objects
        client: IG client to be closed on shutdown
    """
    try:
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
    finally:
        # Ensure client is closed
        await client.close()


async def _create_client_and_strategies(strategy_specs: list) -> tuple[IGClient, list[BaseStrategy]]:
    """
    Create authenticated client and instantiate strategies.
    
    Args:
        strategy_specs: List of either:
            - BaseStrategy subclasses (old format)
            - Tuples of (StrategyClass, kwargs_dict) (new format)
    """
    log.info("Authenticating with IG...")
    client = IGClient()
    await client.start()
    
    strategy_instances = []
    for spec in strategy_specs:
        if isinstance(spec, tuple):
            strategy_class, kwargs = spec
            instance = strategy_class(client, **kwargs)
        else:
            strategy_class = spec
            instance = strategy_class(client)
        
        strategy_instances.append(instance)
    
    return client, strategy_instances


def run_strategies(
    strategy_specs: list,
    log_level: str | None = None,
    setup_logging: bool = True
) -> None:
    """
    Main entry point for running trading strategies.
    
    Args:
        strategy_specs: List of strategy specifications. Each can be:
            - A BaseStrategy subclass (e.g., MyStrategy)
            - A tuple of (StrategyClass, kwargs_dict)
        log_level: Optional log level override
        setup_logging: If True, configure basic logging
    
    Examples:
        run_strategies([MyStrategy, AnotherStrategy])
        
        run_strategies([
            (MyStrategy, {"config_path": "config1.yaml"}),
            (MyStrategy, {"config_path": "config2.yaml"}),
        ])
    """
    # Configure logging
    if setup_logging:
        level = log_level or settings.log_level
        configure_logging(level)
    
    log.info("=" * 70)
    log.info("Tradedesk Strategy Runner")
    log.info("=" * 70)
    log.info("Environment: %s", settings.environment)
    
    # Validate configuration
    try:
        settings.validate()
    except ValueError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)
    
    async def main():
        client = None
        try:
            client, strategy_instances = await _create_client_and_strategies(strategy_specs)
            
            # Run strategies
            log.info("Starting strategies...")
            log.info("-" * 70)
            
            await _run_strategies_async(strategy_instances, client)
        except Exception as e:
            log.exception("Fatal error: %s", e)
            raise
        finally:
            # Ensure client is closed even if there's an error
            if client:
                await client.close()
    
    # Run strategies
    try:
        asyncio.run(main())
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
