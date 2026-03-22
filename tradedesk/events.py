import asyncio
import logging
from abc import ABC
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar, dataclass_transform

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


@dataclass_transform(frozen_default=True, kw_only_default=True)
def event(cls: type[_T]) -> type[_T]:
    """Decorator to mark a class as a domain event."""
    return dataclass(frozen=True, slots=True, kw_only=True)(cls)


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent(ABC):
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@event
class SessionStartedEvent(DomainEvent):
    """Event fired when a new backtest or live session starts."""

    pass


@event
class SessionEndedEvent(DomainEvent):
    """Event fired when a backtest or live session ends."""

    pass


@event
class SessionReadyEvent(DomainEvent):
    """Fired after all SessionStartedEvent handlers complete (warmup + reconciliation done)."""

    pass


class EventDispatcher:
    """Async event dispatcher for domain events.

    Handlers can be sync or async functions. Exceptions in handlers are logged
    but don't stop dispatch to other handlers.
    """

    def __init__(self) -> None:
        self._handlers: dict[
            type[DomainEvent], list[Callable[[DomainEvent], None | Awaitable[None]]]
        ] = defaultdict(list)

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: Callable[..., None | Awaitable[None]],
    ) -> None:
        """Register a handler for an event type.

        Args:
            event_type: The event class to subscribe to
            handler: Sync or async callable that accepts the event
        """
        self._handlers[event_type].append(handler)

    def unsubscribe(
        self,
        event_type: type[DomainEvent],
        handler: Callable[..., None | Awaitable[None]],
    ) -> None:
        """Unregister a handler from an event type."""
        if handler in self._handlers[event_type]:
            self._handlers[event_type].remove(handler)

    async def publish(self, event: DomainEvent) -> None:
        """Dispatch event to all registered handlers.

        Handlers are called sequentially (await each). Exceptions are logged
        and don't prevent other handlers from running.

        Args:
            event: The domain event to dispatch
        """
        handlers = self._handlers[type(event)]
        for handler in handlers:
            try:
                result = handler(event)
                # If handler is async, await it
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(
                    f"Event handler {handler.__name__} failed for {event.__class__.__name__}: {e}",
                    exc_info=True,
                )


# Lazy singleton
_dispatcher: EventDispatcher | None = None


def get_dispatcher() -> EventDispatcher:
    """Get the global event dispatcher instance."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = EventDispatcher()
    return _dispatcher


def reset_dispatcher() -> None:
    """Replace the global dispatcher with a fresh instance.

    Intended for test isolation — ensures no handlers leak between tests.
    """
    global _dispatcher
    _dispatcher = EventDispatcher()
