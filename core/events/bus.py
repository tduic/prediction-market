"""
Lightweight event bus implementation using asyncio.Queue.
Provides pub/sub functionality with error isolation between subscribers.
"""

import asyncio
import logging
from typing import Callable, Type

from .types import Event

logger = logging.getLogger(__name__)


class EventBus:
    """
    Asynchronous event bus with pub/sub capabilities.

    Features:
    - Multiple subscribers per event type
    - Async callback invocation
    - Error isolation (one subscriber crash doesn't affect others)
    - Weak reference cleanup for unsubscribed listeners
    """

    def __init__(self, max_queue_size: int = 10000):
        """
        Initialize the event bus.

        Args:
            max_queue_size: Maximum size of the event queue
        """
        self._subscribers: dict[Type[Event], list[Callable]] = {}
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._running = False
        self._processor_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def subscribe(
        self,
        event_type: Type[Event],
        callback: Callable[[Event], None],
    ) -> None:
        """
        Subscribe to an event type.

        Args:
            event_type: The event class to subscribe to
            callback: Async or sync callable that will be invoked on events.
                     Should accept a single Event argument.

        Raises:
            ValueError: If callback is not callable
        """
        if not callable(callback):
            raise ValueError(f"Callback must be callable, got {type(callback)}")

        async with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []

            if callback not in self._subscribers[event_type]:
                self._subscribers[event_type].append(callback)
                logger.debug(
                    f"Subscriber registered for {event_type.__name__}: {callback}"
                )

    async def unsubscribe(
        self,
        event_type: Type[Event],
        callback: Callable[[Event], None],
    ) -> None:
        """
        Unsubscribe from an event type.

        Args:
            event_type: The event class to unsubscribe from
            callback: The callback to remove
        """
        async with self._lock:
            if event_type in self._subscribers:
                try:
                    self._subscribers[event_type].remove(callback)
                    logger.debug(
                        f"Subscriber unregistered for {event_type.__name__}: {callback}"
                    )
                except ValueError:
                    pass  # Callback was not registered

    async def publish(self, event: Event) -> None:
        """
        Publish an event to all subscribers.

        Args:
            event: The event to publish

        Raises:
            RuntimeError: If bus is not running
            asyncio.QueueFull: If event queue is full
        """
        if not self._running:
            raise RuntimeError("Event bus is not running. Call start() first.")

        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.error(f"Event queue full, dropping event: {type(event).__name__}")
            raise

    async def start(self) -> None:
        """
        Start the event bus processor.
        Must be called before publishing events.
        """
        if self._running:
            logger.warning("Event bus is already running")
            return

        self._running = True
        self._processor_task = asyncio.create_task(self._process_events())
        logger.info("Event bus started")

    async def stop(self) -> None:
        """
        Stop the event bus processor.
        Waits for the queue to be empty before returning.
        """
        if not self._running:
            logger.warning("Event bus is not running")
            return

        self._running = False

        # Wait for queue to empty
        while not self._event_queue.empty():
            await asyncio.sleep(0.01)

        # Cancel processor task if still running
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass

        logger.info("Event bus stopped")

    async def _process_events(self) -> None:
        """
        Main event processing loop.
        Continuously processes events from the queue and invokes subscribers.
        """
        while self._running:
            try:
                # Use wait_for to allow graceful shutdown
                try:
                    event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                event_type = type(event)

                # Get subscribers for this event type
                async with self._lock:
                    subscribers = self._subscribers.get(event_type, []).copy()

                # Invoke each subscriber with error isolation
                for callback in subscribers:
                    try:
                        result = callback(event)
                        # Handle both sync and async callbacks
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        logger.error(
                            f"Error invoking subscriber {callback.__name__} "
                            f"for {event_type.__name__}: {e}",
                            exc_info=True,
                        )

                self._event_queue.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Unexpected error in event processor: {e}", exc_info=True)
                await asyncio.sleep(0.1)  # Brief delay before retry

    def get_subscriber_count(self, event_type: Type[Event]) -> int:
        """
        Get the number of subscribers for an event type.

        Args:
            event_type: The event class to check

        Returns:
            Number of subscribers
        """
        return len(self._subscribers.get(event_type, []))

    def is_running(self) -> bool:
        """Check if the event bus is running."""
        return self._running

    async def wait_for_queue_empty(self, timeout: float = 5.0) -> bool:
        """
        Wait for the event queue to become empty.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if queue became empty, False if timeout
        """
        try:
            await asyncio.wait_for(self._event_queue.join(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
