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

import asyncio
import httpx
import os
from datetime import datetime, UTC
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

SNAPSHOT_DIR = "detection_images"
SNAPSHOT_PATH = "/ISAPI/Streaming/channels/1/picture"

# One retry on transient failure (timeout / 5xx / digest-nonce drift),
# 500ms backoff. Keeps total worst-case latency under ~3s. 401 is treated
# as a permission error, not a transient — see fetch_snapshot.
_FETCH_MAX_ATTEMPTS = 2
_FETCH_BACKOFF_S = 0.5

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


async def _attempt_fetch(url: str, auth, timeout: float = 2.0) -> httpx.Response | None:
    """One round-trip to the camera. Returns the Response on success/auth-fail
    so the caller can inspect the status code; returns None on transport error."""
    try:
        async with httpx.AsyncClient(auth=auth, timeout=timeout) as client:
            return await client.get(url)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        logger.warning(f"[SNAPSHOT] transport error: {exc!r}")
        return None


async def fetch_snapshot(camera_id: str, event_type: str) -> tuple[str, str] | None:
    """Fetch a JPEG from the camera and save it locally.

    Returns ``(public_url, local_path)`` on success, ``None`` on failure.

    Retry policy:
      - One retry with 500ms backoff on transient failure (timeout, 5xx,
        connection error). Catches digest-nonce drift and brief blips.
      - 401 / 403 are treated as permission errors — no retry, but logged
        loudly with the user name and a hint about ISAPI privileges so ops
        can tell creds-vs-permission apart from a transient network issue.
      - Optional Basic-auth fallback when SNAPSHOT_AUTH_FALLBACK=basic
        (default off). Lets ops test cameras with firmware that rejects
        Digest without changing code.
    """
    cam = settings.CAMERAS.get(camera_id)
    if not cam:
        logger.warning(f"[SNAPSHOT] Unknown camera: {camera_id}")
        return None

    url = f"http://{cam['ip']}{SNAPSHOT_PATH}"
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
    filename = f"snap_{event_type}_{camera_id}_{timestamp}.jpg"
    user, password = cam["user"], cam["password"]

    auth_schemes = [("digest", httpx.DigestAuth(user, password))]
    if (getattr(settings, "SNAPSHOT_AUTH_FALLBACK", "") or "").lower() == "basic":
        auth_schemes.append(("basic", httpx.BasicAuth(user, password)))

    response: httpx.Response | None = None
    last_status: int | None = None

    try:
        for scheme_name, auth in auth_schemes:
            for attempt in range(1, _FETCH_MAX_ATTEMPTS + 1):
                response = await _attempt_fetch(url, auth)
                if response is None:
                    last_status = None
                    if attempt < _FETCH_MAX_ATTEMPTS:
                        await asyncio.sleep(_FETCH_BACKOFF_S)
                    continue

                last_status = response.status_code
                if response.status_code == 200:
                    break  # success — exit attempt loop
                if response.status_code in (401, 403):
                    # permission error, no retry on this scheme
                    break
                # 5xx / other → retry on this scheme
                if attempt < _FETCH_MAX_ATTEMPTS:
                    await asyncio.sleep(_FETCH_BACKOFF_S)

            if response is not None and response.status_code == 200:
                break  # success — exit scheme loop

        if response is None or response.status_code != 200:
            if last_status in (401, 403):
                logger.warning(
                    "[SNAPSHOT] %s returned HTTP %s — verify user '%s' has "
                    "ISAPI 'Live View' / 'Image Capture' privileges on the "
                    "camera (admin UI: Configuration → System → User Management)",
                    camera_id, last_status, user,
                )
            else:
                logger.warning(
                    "[SNAPSHOT] %s fetch failed (last status: %s)",
                    camera_id, last_status,
                )
            return None

        image_bytes = response.content
        logger.info(f"[SNAPSHOT] Fetched {filename} ({len(image_bytes)} bytes)")

        filepath = os.path.join(SNAPSHOT_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(image_bytes)
        logger.info(f"[SNAPSHOT] Saved locally → {filepath}")

        return to_public_snapshot_url(filepath), filepath

    except Exception as e:
        logger.error(f"[SNAPSHOT] Failed for {camera_id}: {e!r}")
        return None

