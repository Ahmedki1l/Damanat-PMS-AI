import asyncio
from typing import Any, AsyncGenerator, Callable, Optional
from app.utils.logger import get_logger

logger = get_logger(__name__)

class EventBus:
    def __init__(self):
        # A set of queues, each representing a connected SSE client
        self.subscribers: set[asyncio.Queue] = set()
        self.loop = None
        self._external_publisher: Optional[Callable[[Any], None]] = None

    def set_external_publisher(self, publisher: Optional[Callable[[Any], None]]) -> None:
        """Forward copies to another process without replacing local SSE delivery."""
        self._external_publisher = publisher

    async def subscribe(self) -> AsyncGenerator[Any, None]:
        """Creates a new queue and yields items as they are published."""
        self.loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        self.subscribers.add(queue)
        logger.info(f"[EventBus] New subscriber added. Total subscribers: {len(self.subscribers)}")
        try:
            while True:
                item = await queue.get()
                yield item
        finally:
            self.subscribers.remove(queue)
            logger.info(f"[EventBus] Subscriber removed. Total subscribers: {len(self.subscribers)}")

    def publish(self, data: Any):
        """Sends data to all active queues. Thread-safe."""
        if self._external_publisher is not None:
            try:
                self._external_publisher(data)
            except Exception:
                logger.exception("[EventBus] cross-process notification forwarding failed")
        if not self.subscribers:
            logger.debug("[EventBus] No active subscribers. Dropping event.")
            return

        logger.debug(f"[EventBus] Publishing to {len(self.subscribers)} subscribers")

        def _put(q, d):
            q.put_nowait(d)

        for queue in self.subscribers:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(_put, queue, data)
            else:
                # Fallback if no loop is running yet
                queue.put_nowait(data)

# Global instance to be used across the application
event_bus = EventBus()
