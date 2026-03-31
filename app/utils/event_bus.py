import asyncio
from typing import Any, AsyncGenerator

class EventBus:
    def __init__(self):
        # A set of queues, each representing a connected SSE client
        self.subscribers: set[asyncio.Queue] = set()
        self.loop = None

    async def subscribe(self) -> AsyncGenerator[Any, None]:
        """Creates a new queue and yields items as they are published."""
        self.loop = asyncio.get_running_loop()
        queue = asyncio.Queue()
        self.subscribers.add(queue)
        try:
            while True:
                item = await queue.get()
                yield item
        finally:
            self.subscribers.remove(queue)

    def publish(self, data: Any):
        """Sends data to all active queues. Thread-safe."""
        if not self.subscribers:
            return

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
