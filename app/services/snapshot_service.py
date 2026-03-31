# app/services/snapshot_service.py
"""
Snapshot service — fetches a JPEG snapshot from a Hikvision camera
immediately after a detection event fires.

Endpoint: GET http://{cam_ip}/ISAPI/Streaming/channels/1/picture

Storage modes (controlled by STORAGE_MODE in .env):
  - "local"  → saves to detection_images/ on disk, returns local file path
  - "spaces" → uploads to DigitalOcean Spaces, returns public CDN URL
"""

import httpx
import os
from datetime import datetime
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

SNAPSHOT_DIR = "detection_images"
SNAPSHOT_PATH = "/ISAPI/Streaming/channels/1/picture"

os.makedirs(SNAPSHOT_DIR, exist_ok=True)


async def fetch_snapshot(camera_id: str, event_type: str) -> str | None:
    """
    Fetch a snapshot from the camera.
    Returns a CDN URL (Spaces mode) or local file path (local mode), or None on failure.
    """
    cam = settings.CAMERAS.get(camera_id)
    if not cam:
        logger.warning(f"[SNAPSHOT] Unknown camera: {camera_id}")
        return None

    url = f"http://{cam['ip']}{SNAPSHOT_PATH}"
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"snap_{event_type}_{camera_id}_{timestamp}.jpg"

    try:
        auth = httpx.DigestAuth(cam["user"], cam["password"])
        async with httpx.AsyncClient(auth=auth, timeout=2.0) as client:
            response = await client.get(url)

        if response.status_code != 200:
            logger.warning(f"[SNAPSHOT] {camera_id} returned HTTP {response.status_code}")
            return None

        image_bytes = response.content
        logger.info(f"[SNAPSHOT] Fetched {filename} ({len(image_bytes)} bytes)")

        if settings.STORAGE_MODE == "spaces":
            from app.utils.spaces_client import upload_image
            return upload_image(image_bytes, filename)
        else:
            filepath = os.path.join(SNAPSHOT_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(image_bytes)
            logger.info(f"[SNAPSHOT] Saved locally → {filepath}")
            return filepath

    except Exception as e:
        logger.error(f"[SNAPSHOT] Failed for {camera_id}: {e}")
        return None
