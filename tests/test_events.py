import pytest

from tradedesk.events import DomainEvent, EventDispatcher, event, get_dispatcher


@event
class SampleEvent(DomainEvent):
    """Test event for unit tests."""

    message: str


@event
class AnotherTestEvent(DomainEvent):
    """Another test event."""

    value: int


class TestEventDispatcher:
    """Test suite for EventDispatcher."""

    def test_init(self):
        """Test dispatcher initialization."""
        dispatcher = EventDispatcher()
        assert dispatcher._handlers == {}

    @pytest.mark.asyncio
    async def test_subscribe_and_publish_sync_handler(self):
        """Test subscribing and publishing with synchronous handler."""
        dispatcher = EventDispatcher()
        called_with = []

        def handler(event: SampleEvent):
            called_with.append(event)

        dispatcher.subscribe(SampleEvent, handler)
        event_instance = SampleEvent(message="hello")
        await dispatcher.publish(event_instance)

        assert len(called_with) == 1
        assert called_with[0] == event_instance

    @pytest.mark.asyncio
    async def test_subscribe_and_publish_async_handler(self):
        """Test subscribing and publishing with asynchronous handler."""
        dispatcher = EventDispatcher()
        called_with = []

        async def handler(event: SampleEvent):
            called_with.append(event)

        dispatcher.subscribe(SampleEvent, handler)
        event_instance = SampleEvent(message="hello")
        await dispatcher.publish(event_instance)

        assert len(called_with) == 1
        assert called_with[0] == event_instance

    @pytest.mark.asyncio
    async def test_multiple_handlers_same_event(self):
        """Test multiple handlers for the same event type."""
        dispatcher = EventDispatcher()
        calls = []

        def handler1(event: SampleEvent):
            calls.append(("handler1", event))

        async def handler2(event: SampleEvent):
            calls.append(("handler2", event))

        def handler3(event: SampleEvent):
            calls.append(("handler3", event))

        dispatcher.subscribe(SampleEvent, handler1)
        dispatcher.subscribe(SampleEvent, handler2)
        dispatcher.subscribe(SampleEvent, handler3)

        event_instance = SampleEvent(message="test")
        await dispatcher.publish(event_instance)

        assert len(calls) == 3
        assert calls[0] == ("handler1", event_instance)
        assert calls[1] == ("handler2", event_instance)
        assert calls[2] == ("handler3", event_instance)

    @pytest.mark.asyncio
    async def test_handler_exception_doesnt_stop_dispatch(self):
        """Test that exception in one handler doesn't prevent others from running."""
        dispatcher = EventDispatcher()
        calls = []

        def failing_handler(event: SampleEvent):
            raise ValueError("Handler failed")

        def successful_handler(event: SampleEvent):
            calls.append(event)

        dispatcher.subscribe(SampleEvent, failing_handler)
        dispatcher.subscribe(SampleEvent, successful_handler)

        event_instance = SampleEvent(message="test")
        await dispatcher.publish(event_instance)

        # Second handler should still run despite first one failing
        assert len(calls) == 1
        assert calls[0] == event_instance

    @pytest.mark.asyncio
    async def test_async_handler_exception_doesnt_stop_dispatch(self):
        """Test that exception in async handler doesn't prevent others from running."""
        dispatcher = EventDispatcher()
        calls = []

        async def failing_handler(event: SampleEvent):
            raise ValueError("Async handler failed")

        async def successful_handler(event: SampleEvent):
            calls.append(event)

        dispatcher.subscribe(SampleEvent, failing_handler)
        dispatcher.subscribe(SampleEvent, successful_handler)

        event_instance = SampleEvent(message="test")
        await dispatcher.publish(event_instance)

        # Second handler should still run despite first one failing
        assert len(calls) == 1
        assert calls[0] == event_instance

    @pytest.mark.asyncio
    async def test_different_event_types(self):
        """Test that handlers only receive events they subscribed to."""
        dispatcher = EventDispatcher()
        test_calls = []
        another_calls = []

        def test_handler(event: SampleEvent):
            test_calls.append(event)

        def another_handler(event: AnotherTestEvent):
            another_calls.append(event)

        dispatcher.subscribe(SampleEvent, test_handler)
        dispatcher.subscribe(AnotherTestEvent, another_handler)

        test_event = SampleEvent(message="test")
        another_event = AnotherTestEvent(value=42)

        await dispatcher.publish(test_event)
        await dispatcher.publish(another_event)

        assert len(test_calls) == 1
        assert test_calls[0] == test_event
        assert len(another_calls) == 1
        assert another_calls[0] == another_event

    def test_unsubscribe(self):
        """Test unsubscribing a handler."""
        dispatcher = EventDispatcher()

        def handler(event: SampleEvent):
            pass

        dispatcher.subscribe(SampleEvent, handler)
        assert handler in dispatcher._handlers[SampleEvent]

        dispatcher.unsubscribe(SampleEvent, handler)
        assert handler not in dispatcher._handlers[SampleEvent]

    @pytest.mark.asyncio
    async def test_unsubscribe_prevents_handler_invocation(self):
        """Test that unsubscribed handler is not invoked."""
        dispatcher = EventDispatcher()
        calls = []

        def handler(event: SampleEvent):
            calls.append(event)

        dispatcher.subscribe(SampleEvent, handler)
        dispatcher.unsubscribe(SampleEvent, handler)

        event_instance = SampleEvent(message="test")
        await dispatcher.publish(event_instance)

        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_publish_with_no_handlers(self):
        """Test publishing an event with no registered handlers."""
        dispatcher = EventDispatcher()
        event_instance = SampleEvent(message="test")

        # Should not raise any exception
        await dispatcher.publish(event_instance)


class TestGetDispatcher:
    """Test suite for get_dispatcher singleton function."""

    def test_get_dispatcher_returns_instance(self):
        """Test that get_dispatcher returns an EventDispatcher instance."""
        # Reset singleton for testing
        import tradedesk.events

        tradedesk.events._dispatcher = None

        dispatcher = get_dispatcher()
        assert isinstance(dispatcher, EventDispatcher)

    def test_get_dispatcher_returns_singleton(self):
        """Test that get_dispatcher returns the same instance."""
        # Reset singleton for testing
        import tradedesk.events

        tradedesk.events._dispatcher = None

        dispatcher1 = get_dispatcher()
        dispatcher2 = get_dispatcher()
        assert dispatcher1 is dispatcher2

    @pytest.mark.asyncio
    async def test_singleton_retains_subscriptions(self):
        """Test that singleton dispatcher retains subscriptions across calls."""
        # Reset singleton for testing
        import tradedesk.events

        tradedesk.events._dispatcher = None

        calls = []

        def handler(event: SampleEvent):
            calls.append(event)

        dispatcher1 = get_dispatcher()
        dispatcher1.subscribe(SampleEvent, handler)

        dispatcher2 = get_dispatcher()
        event_instance = SampleEvent(message="test")
        await dispatcher2.publish(event_instance)

        assert len(calls) == 1
        assert calls[0] == event_instance
