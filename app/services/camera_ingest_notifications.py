"""Bounded replay-child to API-parent notification bridge."""

import asyncio
import queue
from typing import Any

from app.utils.event_bus import event_bus
from app.utils.logger import get_logger

logger = get_logger(__name__)


def install_child_notification_forwarder(notification_queue) -> None:
    """Forward already-serialized EventBus payloads without blocking replay."""
    def forward(payload: Any) -> None:
        try:
            notification_queue.put_nowait(payload)
        except queue.Full:
            # Database rows remain the durable source of truth. A slow SSE
            # consumer must never block or fail a camera replay transaction.
            logger.warning("[IngestSpool] replay notification queue full; SSE copy dropped")

    event_bus.set_external_publisher(forward)


async def relay_child_notifications(notification_queue, stop_event) -> None:
    """Publish child copies into the API process's existing local SSE bus."""
    while not stop_event.is_set():
        try:
            payload = await asyncio.to_thread(notification_queue.get, True, 0.25)
        except queue.Empty:
            continue
        except (EOFError, OSError):
            if not stop_event.is_set():
                logger.exception("[IngestSpool] replay notification bridge stopped")
            return
        event_bus.publish(payload)
