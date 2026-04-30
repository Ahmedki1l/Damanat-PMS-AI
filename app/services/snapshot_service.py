# app/services/snapshot_service.py
"""
Snapshot service — fetches a JPEG snapshot from a Hikvision camera
immediately after a detection event fires.

Endpoint: GET http://{cam_ip}/ISAPI/Streaming/channels/1/picture

Storage: always saves to detection_images/ on disk. Returns a tuple of
`(public_url, local_path)` so callers can put the URL into the DB while
keeping the on-disk path for local processing (e.g. forwarding to the
PMS tracking API).
"""

import httpx
import os
from datetime import datetime, UTC
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

SNAPSHOT_DIR = "detection_images"
SNAPSHOT_PATH = "/ISAPI/Streaming/channels/1/picture"

os.makedirs(SNAPSHOT_DIR, exist_ok=True)


def to_public_snapshot_url(local_path: str | None) -> str | None:
    """Convert a local snapshot filesystem path (or just a filename) into the
    full public URL served by the /snapshots StaticFiles mount.

    Returns None for empty input. Drops directory components — only the
    filename matters because /snapshots is rooted at SNAPSHOT_DIR.
    """
    if not local_path:
        return None
    filename = os.path.basename(local_path)
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    return f"{base}/pms-ai/snapshots/{filename}" if base else f"/pms-ai/snapshots/{filename}"


async def fetch_snapshot(camera_id: str, event_type: str) -> tuple[str, str] | None:
    """Fetch a JPEG from the camera and save it locally.

    Returns ``(public_url, local_path)`` on success, ``None`` on failure.
    """
    cam = settings.CAMERAS.get(camera_id)
    if not cam:
        logger.warning(f"[SNAPSHOT] Unknown camera: {camera_id}")
        return None

    url = f"http://{cam['ip']}{SNAPSHOT_PATH}"
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
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

        filepath = os.path.join(SNAPSHOT_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        logger.info(f"[SNAPSHOT] Saved locally → {filepath}")

        return to_public_snapshot_url(filepath), filepath

    except Exception as e:
        logger.error(f"[SNAPSHOT] Failed for {camera_id}: {e}")
        return None

