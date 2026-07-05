# app/utils/core_backend_client.py
"""
Async HTTP client for posting events to the Node.js core backend.
Auth: static X-Service-Key header (no JWT).
Fire-and-forget safe: all failures are logged and never block camera event processing.
"""

import base64
import json
import os
import uuid
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

async def _deliver_anpr_payload(body: dict) -> str:
    """POST one ANPR payload to the VA tracking API, in a single attempt.

    Returns one of:
      "ok"    — VA acked (HTTP 200/201).
      "drop"  — VA rejected it permanently (HTTP 4xx); never retry, discard.
      "retry" — transient failure (connect error / timeout / 5xx); try later.

    Never raises. Deliberately ONE attempt with no inline retry loop: this call
    is awaited on the exit webhook (handle_anpr_event) and inside the entry-burst
    flusher, so an inline retry+backoff would add multi-second latency there when
    VA is down. The background spool drain provides the retry cadence instead."""
    url = f"{settings.PMS_API_URL}/api/anpr/event"
    plate = body.get("plate")
    direction = body.get("direction")
    has_img = bool(body.get("image_base64"))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                url, json=body, headers={"Content-Type": "application/json"}
            )
    except httpx.ConnectError:
        logger.warning(f"[PMS] Unreachable — could not forward plate={plate}")
        return "retry"
    except Exception as e:
        logger.warning(f"[PMS] Forward failed for plate={plate}: {e}")
        return "retry"

    if resp.status_code in (200, 201):
        logger.info(
            f"[PMS] Plate forwarded: {plate} ({direction}) "
            f"image={'yes' if has_img else 'no'}"
        )
        return "ok"
    if 400 <= resp.status_code < 500:
        # Client error — the payload itself is bad; retrying can never succeed,
        # so drop it rather than spooling a poison item that wedges the drain.
        logger.warning(
            f"[PMS] VA rejected plate={plate} ({direction}) HTTP "
            f"{resp.status_code}: {resp.text[:200]} — dropping (not retryable)"
        )
        return "drop"
    logger.warning(
        f"[PMS] POST /api/anpr/event -> HTTP {resp.status_code}: "
        f"{resp.text[:200]} — will retry"
    )
    return "retry"


def _spool_payload(body: dict) -> None:
    """Persist an undelivered ANPR forward to disk so a background drain can
    re-POST it once VA is reachable again — instead of losing the image."""
    try:
        d = settings.PMS_FORWARD_SPOOL_DIR
        os.makedirs(d, exist_ok=True)
        record = dict(body)
        record["_spooled_at"] = datetime.now().isoformat()
        fname = (
            f"anpr_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_"
            f"{uuid.uuid4().hex[:8]}.json"
        )
        tmp = os.path.join(d, fname + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.replace(tmp, os.path.join(d, fname))
        logger.warning(
            f"[PMS] Spooled undelivered plate={body.get('plate')} "
            f"({body.get('direction')}) for later retry"
        )
    except Exception as e:
        logger.warning(f"[PMS] Failed to spool plate={body.get('plate')}: {e}")


async def notify_pms_anpr(
    plate: str,
    direction: str,
    image_path: Optional[str] = None,
) -> None:
    """
    Forward ANPR detection to the PMS tracking API.
    Sends plate + direction + base64-encoded snapshot image.
    One inline attempt; on a transient failure the payload is spooled to disk and
    re-POSTed by the background drain, so the image is not lost when VA is briefly
    unreachable. Never blocks camera event processing (all failures are logged /
    spooled, never raised).
    """
    if not settings.PMS_API_URL:
        return

    image_base64 = ""

    if image_path:
        try:
            if image_path.startswith("http"):
                # CDN URL — download the image first
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(image_path)
                    if resp.status_code == 200:
                        image_base64 = base64.b64encode(resp.content).decode("ascii")
                    else:
                        logger.warning(f"[PMS] Image download failed: HTTP {resp.status_code} from {image_path}")
            elif os.path.exists(image_path):
                # Local file
                with open(image_path, "rb") as f:
                    image_base64 = base64.b64encode(f.read()).decode("ascii")
            else:
                logger.warning(f"[PMS] Image path does not exist: {image_path}")
        except Exception as e:
            logger.warning(f"[PMS] Failed to encode image for plate={plate}: {e}")
    else:
        logger.warning(f"[PMS] No snapshot available for plate={plate} — sending without image")

    body = {
        "plate": plate,
        "direction": direction,
        "image_base64": image_base64,
    }

    status = await _deliver_anpr_payload(body)
    if status == "retry":
        # Transient — keep the image for the background drain to redeliver.
        _spool_payload(body)
    # "ok" → delivered; "drop" → VA rejected it permanently, discard (no spool).


async def drain_pms_forward_spool() -> None:
    """Re-POST spooled ANPR forwards to VA; delete each on success.

    Runs periodically from the background drain task. Processes oldest-first
    (spool filenames are timestamped). Delivered ("ok") and permanently-rejected
    ("drop", HTTP 4xx) payloads are removed and the drain moves on — a bad
    payload never wedges the queue behind it. Only a transient failure ("retry",
    VA still down) stops the pass so ordering is preserved and VA isn't hammered.
    Payloads older than ``PMS_FORWARD_SPOOL_MAX_AGE_SECONDS`` are dropped. No-op
    when forwarding is disabled or the spool dir is empty."""
    if not settings.PMS_API_URL:
        return
    d = settings.PMS_FORWARD_SPOOL_DIR
    if not os.path.isdir(d):
        return
    now = datetime.now()
    max_age = float(settings.PMS_FORWARD_SPOOL_MAX_AGE_SECONDS)
    for fname in sorted(os.listdir(d)):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(d, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except Exception:
            # Corrupt/partial file — remove so it doesn't wedge the drain.
            try:
                os.remove(fpath)
            except OSError:
                pass
            continue

        # Drop stale payloads. Parse age and expiry independently of the delete
        # so a bad timestamp or a failed remove still skips (never re-POSTs) the
        # stale item rather than falling through to deliver it.
        spooled_at = record.pop("_spooled_at", None)
        age = None
        if spooled_at:
            try:
                age = (now - datetime.fromisoformat(spooled_at)).total_seconds()
            except Exception:
                age = None
        if age is not None and age > max_age:
            try:
                os.remove(fpath)
                logger.warning(
                    f"[PMS] Dropped stale spooled plate={record.get('plate')} "
                    f"(age {age:.0f}s > {max_age:.0f}s)"
                )
            except OSError:
                pass
            continue

        body = {
            "plate": record.get("plate"),
            "direction": record.get("direction"),
            "image_base64": record.get("image_base64", ""),
        }
        status = await _deliver_anpr_payload(body)
        if status in ("ok", "drop"):
            # Delivered or permanently rejected — remove and keep draining.
            try:
                os.remove(fpath)
            except OSError:
                pass
            continue
        # "retry" — VA still unreachable; leave the rest for the next interval.
        break

