# app/utils/core_backend_client.py
"""
Async HTTP client for posting events to the Node.js core backend.
Auth: static X-Service-Key header (no JWT).
Fire-and-forget safe: all failures are logged and never block camera event processing.
"""

from datetime import datetime
from typing import Optional
import httpx
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if settings.NODEBACK_SERVICE_KEY:
        h["X-Service-Key"] = settings.NODEBACK_SERVICE_KEY
    return h


async def _post(path: str, body: dict) -> Optional[dict]:
    """
    POST to the Node.js backend with the service key.
    Returns response JSON dict on 200/201, None on any failure.
    """
    if not settings.NODEBACK_URL:
        return None

    url = f"{settings.NODEBACK_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=body, headers=_headers())
            if resp.status_code in (200, 201):
                return resp.json()
            logger.warning(f"[NodeBack] POST {path} → HTTP {resp.status_code}: {resp.text[:200]}")
            return None
    except httpx.ConnectError:
        logger.warning(f"[NodeBack] Unreachable — could not POST {path}")
        return None
    except Exception as e:
        logger.warning(f"[NodeBack] POST {path} error: {e}")
        return None


# ── Occupancy ──────────────────────────────────────────────────────────────

async def notify_occupancy_entry(zone_id: str, camera_id: str) -> Optional[dict]:
    """
    Notify Node.js that a vehicle entered a zone.
    Returns the response data dict (contains currentCount, maxCapacity, isFull, percentage).
    """
    if not settings.NODEBACK_URL or not settings.NODEBACK_SITE_ID:
        return None
    result = await _post(
        f"/api/v1/sites/{settings.NODEBACK_SITE_ID}/occupancy/entry",
        {"zoneId": zone_id, "cameraId": camera_id},
    )
    if result:
        data = result.get("data", {})
        logger.info(
            f"[NodeBack] Occupancy entry zone={zone_id} "
            f"count={data.get('currentCount')}/{data.get('maxCapacity')} "
            f"full={data.get('isFull')}"
        )
        return data
    return None


async def notify_occupancy_exit(zone_id: str, camera_id: str) -> Optional[dict]:
    """
    Notify Node.js that a vehicle exited a zone.
    Returns the response data dict.
    """
    if not settings.NODEBACK_URL or not settings.NODEBACK_SITE_ID:
        return None
    result = await _post(
        f"/api/v1/sites/{settings.NODEBACK_SITE_ID}/occupancy/exit",
        {"zoneId": zone_id, "cameraId": camera_id},
    )
    if result:
        data = result.get("data", {})
        logger.info(
            f"[NodeBack] Occupancy exit zone={zone_id} "
            f"count={data.get('currentCount')}/{data.get('maxCapacity')}"
        )
        return data
    return None


# ── Parking Times (ANPR) ───────────────────────────────────────────────────

async def notify_entry(
    plate: str,
    camera_id: str,
    event_time: datetime,
    vehicle_type: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    """Push a vehicle entry event to the Node.js backend (MongoDB parking-times)."""
    if not settings.NODEBACK_URL or not settings.NODEBACK_SITE_ID:
        return

    body: dict = {
        "plateNumber": plate,
        "cameraId": camera_id,
        "entryTime": event_time.isoformat(),
    }
    if vehicle_type:
        body["vehicleType"] = vehicle_type
    if image_url:
        body["imageUrl"] = image_url

    result = await _post(f"/api/v1/sites/{settings.NODEBACK_SITE_ID}/parking-times/entry", body)
    if result is not None:
        logger.info(f"[NodeBack] Entry recorded plate={plate}")


async def notify_exit(
    plate: str,
    camera_id: str,
    event_time: datetime,
    image_url: Optional[str] = None,
) -> None:
    """Push a vehicle exit event to the Node.js backend (MongoDB parking-times)."""
    if not settings.NODEBACK_URL or not settings.NODEBACK_SITE_ID:
        return

    body: dict = {
        "plateNumber": plate,
        "cameraId": camera_id,
        "exitTime": event_time.isoformat(),
    }
    if image_url:
        body["imageUrl"] = image_url

    result = await _post(f"/api/v1/sites/{settings.NODEBACK_SITE_ID}/parking-times/exit", body)
    if result is not None:
        logger.info(f"[NodeBack] Exit recorded plate={plate}")


# ── PMS Tracking API (plate forwarding) ────────────────────────────────

async def notify_pms_anpr(
    plate: str,
    direction: str,
    image_path: Optional[str] = None,
) -> None:
    """
    Forward ANPR detection to the PMS tracking API.
    Sends plate + direction + base64-encoded snapshot image.
    Fire-and-forget: failures are logged but never block camera event processing.
    """
    if not settings.PMS_API_URL:
        return

    import base64
    import os

    image_base64 = ""

    if image_path:
        try:
            if image_path.startswith("http"):
                # CDN URL — download the image first
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(image_path)
                    if resp.status_code == 200:
                        image_base64 = base64.b64encode(resp.content).decode("ascii")
            elif os.path.exists(image_path):
                # Local file
                with open(image_path, "rb") as f:
                    image_base64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            logger.warning(f"[PMS] Failed to encode image for plate={plate}: {e}")

    body = {
        "plate": plate,
        "direction": direction,
        "image_base64": image_base64,
    }

    url = f"{settings.PMS_API_URL}/api/anpr/event"
    logger.info(f"[PMS→YOLO] Sending plate={plate} direction={direction} to {url}")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=body, headers={"Content-Type": "application/json"})
            if resp.status_code in (200, 201):
                logger.info(f"[PMS] Plate forwarded: {plate} ({direction})")
            else:
                logger.warning(f"[PMS] POST /api/anpr/event → HTTP {resp.status_code}: {resp.text[:200]}")
    except httpx.ConnectError:
        logger.warning(f"[PMS] Unreachable — could not forward plate={plate}")
    except Exception as e:
        logger.warning(f"[PMS] Forward failed for plate={plate}: {e}")
