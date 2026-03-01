# app/services/camera_poller.py
"""
Camera polling service — pulls events from Hikvision cameras via ISAPI alertStream.

Instead of waiting for cameras to push HTTP webhooks (which requires network access
from cameras to this machine), this service connects TO the cameras and listens
on their alertStream endpoint in real-time.

Endpoint: GET http://{cam_ip}/ISAPI/Event/notification/alertStream
Protocol: HTTP multipart stream — camera sends XML event chunks continuously.
"""

import asyncio
import httpx
from app.config import settings
from app.services.event_parser import parse_camera_event
from app.services.event_dispatcher import dispatch_event
from app.database import SessionLocal
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Reconnect delay in seconds (doubles on each failure, max 60s)
_MIN_BACKOFF = 3
_MAX_BACKOFF = 60

# Boundary marker used by Hikvision multipart streams
_BOUNDARY_MARKER = b"--boundary"
_EVENT_START     = b"<EventNotificationAlert"
_EVENT_END       = b"</EventNotificationAlert>"


async def _poll_camera(cam_id: str, cam: dict):
    """
    Opens a persistent connection to one camera's alertStream and dispatches
    events as they arrive. Reconnects automatically on failure.
    """
    url = f"http://{cam['ip']}/ISAPI/Event/notification/alertStream"
    backoff = _MIN_BACKOFF

    while True:
        logger.info(f"📡 Connecting to alertStream: {cam_id} ({cam['ip']})")
        try:
            # Hikvision requires HTTP Digest Auth (not Basic)
            auth = httpx.DigestAuth(cam["user"], cam["password"])
            async with httpx.AsyncClient(auth=auth, timeout=None) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        logger.warning(
                            f"⚠️  {cam_id} alertStream returned HTTP {response.status_code}"
                        )
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _MAX_BACKOFF)
                        continue

                    logger.info(f"✅ {cam_id} alertStream connected — listening for events...")
                    backoff = _MIN_BACKOFF  # reset on success

                    buffer = b""
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        buffer += chunk
                        # Extract complete XML events from the buffer
                        while _EVENT_START in buffer and _EVENT_END in buffer:
                            start = buffer.find(_EVENT_START)
                            end   = buffer.find(_EVENT_END) + len(_EVENT_END)
                            xml_bytes = buffer[start:end]
                            buffer = buffer[end:]
                            await _handle_event(xml_bytes, cam_id, cam["ip"])

        except httpx.ConnectError:
            logger.warning(f"❌ {cam_id} — connection refused. Retry in {backoff}s")
        except httpx.ReadTimeout:
            logger.warning(f"⏱  {cam_id} — read timeout. Reconnecting...")
        except Exception as e:
            logger.error(f"❌ {cam_id} — unexpected error: {e}", exc_info=True)

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


async def _handle_event(xml_bytes: bytes, cam_id: str, cam_ip: str):
    """Parse and dispatch a single XML event from the stream."""
    try:
        logger.debug(f"🔍 RAW XML from {cam_id}:\n{xml_bytes.decode('utf-8', errors='replace')}")
        event = parse_camera_event(xml_bytes, cam_ip, "application/xml")
        logger.info(
            f"📥 {cam_id} | type={event.event_type} "
            f"target={event.detection_target} zone={event.region_id}"
        )

        # Use a fresh DB session per event
        db = SessionLocal()
        try:
            from datetime import datetime
            from app.models.camera_event import CameraEvent
            db.add(CameraEvent(
                camera_id=event.camera_id,
                device_serial=event.device_serial,
                channel_id=event.channel_id,
                event_type=event.event_type,
                detection_target=event.detection_target,
                region_id=event.region_id,
                channel_name=event.channel_name,
                trigger_time=event.trigger_time,
                raw_payload=xml_bytes.decode("utf-8", errors="replace"),
                created_at=datetime.utcnow(),
            ))
            db.commit()
            await dispatch_event(event, db)
        finally:
            db.close()

    except Exception as e:
        logger.error(f"Event handling error from {cam_id}: {e}", exc_info=True)


async def start_camera_polling(cameras: dict):
    """
    Launch one polling task per camera concurrently.
    Called once at backend startup.
    """
    if not cameras:
        logger.warning("No cameras configured — polling disabled.")
        return

    logger.info(f"🚀 Starting camera polling for {len(cameras)} cameras...")
    tasks = [
        asyncio.create_task(_poll_camera(cam_id, cam), name=f"poller-{cam_id}")
        for cam_id, cam in cameras.items()
    ]
    # Run all pollers concurrently (they loop forever internally)
    await asyncio.gather(*tasks, return_exceptions=True)
