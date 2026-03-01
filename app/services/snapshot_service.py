# app/services/snapshot_service.py
"""
Snapshot service — fetches a JPEG snapshot from a Hikvision camera
immediately after a detection event fires.

Endpoint: GET http://{cam_ip}/ISAPI/Streaming/channels/1/picture
Saves to:  detection_images/snap_{event_type}_{camera_id}_{timestamp}.jpg
"""

import httpx
import os
from datetime import datetime
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

SNAPSHOT_DIR = "detection_images"
SNAPSHOT_PATH = "/ISAPI/Streaming/channels/1/picture"

# Ensure folder exists on import
os.makedirs(SNAPSHOT_DIR, exist_ok=True)


async def fetch_snapshot(camera_id: str, event_type: str) -> str | None:
    """
    Fetch a snapshot from the camera and save it locally.
    Returns the saved file path, or None if it failed.
    """
    cam = settings.CAMERAS.get(camera_id)
    if not cam:
        logger.warning(f"[SNAPSHOT] Unknown camera: {camera_id}")
        return None

    url = f"http://{cam['ip']}{SNAPSHOT_PATH}"
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
    filename = f"snap_{event_type}_{camera_id}_{timestamp}.jpg"
    filepath = os.path.join(SNAPSHOT_DIR, filename)

    try:
        auth = httpx.DigestAuth(cam["user"], cam["password"])
        async with httpx.AsyncClient(auth=auth, timeout=10) as client:
            response = await client.get(url)
            if response.status_code == 200:
                with open(filepath, "wb") as f:
                    f.write(response.content)
                logger.info(f"[SNAPSHOT] Saved {filename} ({len(response.content)} bytes)")
                return filepath
            else:
                logger.warning(f"[SNAPSHOT] {camera_id} returned HTTP {response.status_code}")
                return None
    except Exception as e:
        logger.error(f"[SNAPSHOT] Failed for {camera_id}: {e}")
        return None
