# app/utils/core_backend_client.py
"""
Async HTTP client for posting events to the Node.js core backend.
Auth: static X-Service-Key header (no JWT).
Legacy forwards use a durable spool; authoritative exits surface a retry only
when both live delivery and durable spooling fail.
"""

import base64
import json
import os
import uuid
from datetime import datetime
from typing import Optional

import httpx
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.services.event_parser import normalize_plate
from app.utils.logger import get_logger

logger = get_logger(__name__)

_ENTRY_V2_RETRYABLE_CLIENT_STATUSES = frozenset(
    {401, 403, 404, 405, 408, 425, 429}
)
_reported_quarantined_spools: set[str] = set()
_reported_retrying_spools: set[str] = set()
_reported_directory_fsync_unavailable = False
_http_client: Optional[httpx.AsyncClient] = None


class AnprForwardUnavailable(RuntimeError):
    """VA did not receive an active-V2 exit and no durable retry was saved."""


class MalformedSpoolRecord(ValueError):
    """A spool file is readable but cannot be interpreted as a JSON record."""


async def start_core_backend_http_client() -> None:
    """Create one app-lifetime client for NodeBack and legacy VA traffic."""
    global _http_client
    if _http_client is None and (settings.NODEBACK_URL or settings.PMS_API_URL):
        _http_client = httpx.AsyncClient(timeout=10)


async def close_core_backend_http_client() -> None:
    """Close and detach the shared NodeBack/legacy VA client."""
    global _http_client
    client = _http_client
    _http_client = None
    if client is not None:
        await client.aclose()


async def _http_post(url: str, **kwargs) -> httpx.Response:
    """POST with the app client, retaining a scoped fallback for scripts/tests."""
    if _http_client is not None:
        return await _http_client.post(url, **kwargs)
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.post(url, **kwargs)


async def _http_get(url: str, **kwargs) -> httpx.Response:
    """GET with the app client, retaining a scoped fallback for scripts/tests."""
    if _http_client is not None:
        return await _http_client.get(url, **kwargs)
    async with httpx.AsyncClient(timeout=10) as client:
        return await client.get(url, **kwargs)


def _encode_local_image(image_path: str) -> str:
    """Read and base64-encode one local image outside the asyncio loop."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("ascii")


def _encode_image_bytes(image_data: bytes) -> str:
    """Base64-encode an already downloaded image outside the asyncio loop."""
    return base64.b64encode(image_data).decode("ascii")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if settings.NODEBACK_SERVICE_KEY:
        h["X-Service-Key"] = settings.NODEBACK_SERVICE_KEY
    return h


def _pms_tracking_headers() -> dict:
    """Authenticate the PMS-AI -> VA tracking boundary when V2 is enabled."""
    headers = {"Content-Type": "application/json"}
    service_key = getattr(settings, "ENTRY_V2_SERVICE_KEY", "")
    if service_key:
        headers["X-Service-Key"] = service_key
    return headers


def _is_active_v2_exit(payload: dict) -> bool:
    return (
        settings.ENTRY_V2_MODE in {"shadow", "authoritative"}
        and str(payload.get("direction") or "").strip().lower() == "exit"
    )


def _normalized_plate_key(value: object) -> str:
    """Normalize an acknowledgement plate without weakening exact identity."""
    raw_value = str(value or "").strip().upper()
    return normalize_plate(raw_value) or raw_value


def _parse_aware_transport_timestamp(value: object) -> datetime:
    """Parse one offset-aware timestamp returned across the PMS/VA boundary."""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw_value = str(value or "").strip()
        if raw_value.endswith(("Z", "z")):
            raw_value = raw_value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw_value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone offset")
    return parsed


def _active_v2_exit_ack_error(response: httpx.Response, body: dict) -> str:
    """Return why a nominal 2xx did not prove that VA applied this exit."""
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return "VA exit response is not valid JSON"
    if not isinstance(payload, dict):
        return "VA exit response JSON must be an object"
    if payload.get("status") != "ok":
        return "VA exit response status is not ok"

    expected_plate = _normalized_plate_key(body.get("plate"))
    received_plate = _normalized_plate_key(payload.get("plate"))
    if not expected_plate or received_plate != expected_plate:
        return "VA exit response plate does not match submitted plate"

    expected_direction = str(body.get("direction") or "").strip().lower()
    received_direction = str(payload.get("direction") or "").strip().lower()
    if received_direction != expected_direction:
        return "VA exit response direction does not match submitted direction"

    response_timestamp = payload.get("timestamp")
    request_timestamp = body.get("captured_at")
    if response_timestamp is None or request_timestamp is None:
        return "VA exit response is missing the source timestamp acknowledgement"
    try:
        expected_timestamp = _parse_aware_transport_timestamp(request_timestamp)
        received_timestamp = _parse_aware_transport_timestamp(response_timestamp)
    except (OverflowError, TypeError, ValueError):
        return "VA exit response timestamp is invalid"
    if received_timestamp != expected_timestamp:
        return "VA exit response timestamp does not match submitted source time"
    return ""


def _list_spool_paths(directory: str) -> list[str]:
    """List spool records synchronously; callers run this in a worker thread."""
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".json")
    )


def _read_spool_record(path: str) -> dict:
    """Read one spool record, distinguishing malformed data from I/O failure."""
    try:
        with open(path, "r", encoding="utf-8") as spool_file:
            record = json.load(spool_file)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedSpoolRecord(str(exc)) from exc
    if not isinstance(record, dict):
        raise MalformedSpoolRecord("JSON root must be an object")
    return record


def _quarantine_spool_file(path: str) -> str:
    """Atomically move malformed evidence aside without overwriting prior files."""
    quarantine_path = f"{path}.{uuid.uuid4().hex}.corrupt"
    os.replace(path, quarantine_path)
    return quarantine_path


def _remove_spool_file(path: str) -> None:
    """Remove one completed spool record from a worker thread."""
    os.remove(path)


def _fsync_directory(directory: str) -> None:
    """Persist a rename on platforms that expose directory file descriptors."""
    global _reported_directory_fsync_unavailable
    if os.name == "nt":
        if not _reported_directory_fsync_unavailable:
            logger.warning(
                "[PMS] Directory fsync is unavailable on Windows; the spool file "
                "content is synced but rename durability is platform-dependent"
            )
            _reported_directory_fsync_unavailable = True
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(directory, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


async def _post(path: str, body: dict) -> Optional[dict]:
    """
    POST to the Node.js backend with the service key.
    Returns response JSON dict on 200/201, None on any failure.
    """
    if not settings.NODEBACK_URL:
        return None

    url = f"{settings.NODEBACK_URL}{path}"
    try:
        resp = await _http_post(url, json=body, headers=_headers())
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

async def _deliver_anpr_payload(
    body: dict,
    *,
    log_retry_failure: bool = True,
) -> str:
    """POST one ANPR payload to the VA tracking API, in a single attempt.

    Returns one of:
      "ok"    — VA acked; active-V2 exits also passed semantic validation.
      "drop"  — VA rejected the payload permanently; never retry, discard.
      "retry" — transport, server, or V2 boundary failure; try later.

    Never raises. Deliberately ONE attempt with no inline retry loop: this call
    is awaited on the exit webhook (handle_anpr_event) and inside the entry-burst
    flusher, so an inline retry+backoff would add multi-second latency there when
    VA is down. The background spool drain provides the retry cadence instead."""
    url = f"{settings.PMS_API_URL}/api/anpr/event"
    plate = body.get("plate")
    direction = body.get("direction")
    has_img = bool(body.get("image_base64"))
    try:
        resp = await _http_post(
            url,
            json=body,
            headers=_pms_tracking_headers(),
        )
    except httpx.ConnectError:
        if log_retry_failure:
            logger.warning(f"[PMS] Unreachable — could not forward plate={plate}")
        return "retry"
    except Exception as e:
        if log_retry_failure:
            logger.warning(f"[PMS] Forward failed for plate={plate}: {e}")
        return "retry"

    if resp.status_code in (200, 201):
        if _is_active_v2_exit(body):
            ack_error = _active_v2_exit_ack_error(resp, body)
            if ack_error:
                if log_retry_failure:
                    logger.warning(
                        "[PMS] %s plate=%s direction=%s — will retry",
                        ack_error,
                        plate,
                        direction,
                    )
                return "retry"
        logger.info(
            f"[PMS] Plate forwarded: {plate} ({direction}) "
            f"image={'yes' if has_img else 'no'}"
        )
        return "ok"
    if (
        settings.ENTRY_V2_MODE in {"shadow", "authoritative"}
        and resp.status_code in _ENTRY_V2_RETRYABLE_CLIENT_STATUSES
    ):
        if log_retry_failure:
            logger.warning(
                f"[PMS] VA boundary rejected plate={plate} ({direction}) HTTP "
                f"{resp.status_code}: {resp.text[:200]} — will retry"
            )
        return "retry"
    if 400 <= resp.status_code < 500:
        logger.warning(
            f"[PMS] VA rejected plate={plate} ({direction}) HTTP "
            f"{resp.status_code}: {resp.text[:200]} — dropping (not retryable)"
        )
        return "drop"
    if log_retry_failure:
        logger.warning(
            f"[PMS] POST /api/anpr/event -> HTTP {resp.status_code}: "
            f"{resp.text[:200]} — will retry"
        )
    return "retry"


def _spool_payload(body: dict) -> bool:
    """Persist an undelivered ANPR forward for background redelivery.

    Active-V2 exits are metadata-only: VA uses plate plus source timestamp to
    remove identity and ignores exit pixels, so retaining base64 would consume
    disk without adding recovery value.
    """
    tmp: Optional[str] = None
    try:
        d = settings.PMS_FORWARD_SPOOL_DIR
        os.makedirs(d, exist_ok=True)
        record = dict(body)
        if _is_active_v2_exit(record):
            record["image_base64"] = ""
        record["_spooled_at"] = datetime.now().isoformat()
        fname = (
            f"anpr_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_"
            f"{uuid.uuid4().hex[:8]}.json"
        )
        tmp = os.path.join(d, fname + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, os.path.join(d, fname))
        _fsync_directory(d)
        logger.warning(
            f"[PMS] Spooled undelivered plate={body.get('plate')} "
            f"({body.get('direction')}) for later retry"
        )
        return True
    except Exception as e:
        logger.warning(f"[PMS] Failed to spool plate={body.get('plate')}: {e}")
        if tmp is not None:
            try:
                os.remove(tmp)
            except FileNotFoundError:
                pass
            except OSError as cleanup_exc:
                logger.warning(
                    "[PMS] Could not remove incomplete spool temp %s: %s",
                    tmp,
                    cleanup_exc,
                )
        return False


async def notify_pms_anpr(
    plate: str,
    direction: str,
    image_path: Optional[str] = None,
    captured_at: Optional[datetime] = None,
) -> None:
    """
    Forward ANPR detection to the PMS tracking API.
    Sends plate + direction + base64-encoded snapshot image.  ``captured_at``
    is the source camera's timezone-aware event time; exit delivery and spool
    replay use it instead of HTTP arrival time so a delayed old exit cannot
    close a newer visit in VA.
    One inline attempt; on a transient failure the payload is spooled to disk and
    re-POSTed by the background drain. Active-V2 exits raise
    ``AnprForwardUnavailable`` only when neither VA delivery nor the durable
    metadata spool succeeded, allowing the authoritative camera request to
    return 503 and heal through an idempotent retry.
    """
    active_v2_exit = (
        settings.ENTRY_V2_MODE in {"shadow", "authoritative"}
        and direction.strip().lower() == "exit"
    )
    if not settings.PMS_API_URL:
        if active_v2_exit:
            raise AnprForwardUnavailable("VA peer URL is not configured")
        return

    image_base64 = ""

    if image_path:
        try:
            if image_path.startswith("http"):
                # CDN URL — download the image first
                resp = await _http_get(image_path)
                if resp.status_code == 200:
                    image_base64 = await run_in_threadpool(
                        _encode_image_bytes,
                        resp.content,
                    )
                else:
                    logger.warning(f"[PMS] Image download failed: HTTP {resp.status_code} from {image_path}")
            else:
                image_base64 = await run_in_threadpool(
                    _encode_local_image,
                    image_path,
                )
        except FileNotFoundError:
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
    if captured_at is not None:
        if captured_at.tzinfo is None or captured_at.utcoffset() is None:
            raise ValueError("captured_at must include a timezone offset")
        body["captured_at"] = captured_at.isoformat()

    status = await _deliver_anpr_payload(body)
    if status == "retry":
        # Keep recoverable transport/boundary failures for background delivery.
        spooled = await run_in_threadpool(_spool_payload, body)
        if not spooled and active_v2_exit:
            raise AnprForwardUnavailable(
                "VA exit delivery failed and the retry spool write failed"
            )
    # "ok" → delivered; "drop" → VA rejected it permanently, discard (no spool).


async def drain_pms_forward_spool() -> None:
    """Re-POST spooled ANPR forwards to VA; delete each on success.

    Runs periodically from the background drain task. Processes oldest-first
    (spool filenames are timestamped). Delivered ("ok") and permanently-rejected
    ("drop") payloads are removed and the drain moves on — a bad payload never
    wedges the queue behind it. A recoverable failure ("retry") stops the pass
    so ordering is preserved and VA isn't hammered while unavailable or
    misconfigured.
    Legacy/non-exit payloads older than ``PMS_FORWARD_SPOOL_MAX_AGE_SECONDS``
    are dropped. Active-V2 exits never age out: their source time makes delayed
    replay safe across re-entry, and dropping one can leave stale VA identity.
    Malformed JSON is atomically renamed with a ``.corrupt`` suffix for operator
    recovery; transient scan/read failures retain the original record.
    No-op when forwarding is disabled or the spool dir is empty.
    """
    if not settings.PMS_API_URL:
        return
    d = settings.PMS_FORWARD_SPOOL_DIR
    try:
        spool_paths = await run_in_threadpool(_list_spool_paths, d)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("[PMS] Could not scan forward spool %s: %s", d, exc)
        return

    present_spools = set(spool_paths)
    _reported_quarantined_spools.intersection_update(present_spools)
    _reported_retrying_spools.intersection_update(present_spools)
    now = datetime.now()
    max_age = float(settings.PMS_FORWARD_SPOOL_MAX_AGE_SECONDS)
    for fpath in spool_paths:
        try:
            record = await run_in_threadpool(_read_spool_record, fpath)
        except FileNotFoundError:
            # Another worker or operator completed this record after the scan.
            _reported_quarantined_spools.discard(fpath)
            _reported_retrying_spools.discard(fpath)
            continue
        except MalformedSpoolRecord as exc:
            try:
                quarantine_path = await run_in_threadpool(
                    _quarantine_spool_file,
                    fpath,
                )
            except FileNotFoundError:
                continue
            except OSError as quarantine_exc:
                logger.error(
                    "[PMS] Malformed spool %s could not be quarantined; retained "
                    "for operator recovery: %s",
                    fpath,
                    quarantine_exc,
                )
                break
            logger.error(
                "[PMS] Quarantined malformed forward spool %s as %s: %s",
                fpath,
                quarantine_path,
                exc,
            )
            _reported_quarantined_spools.discard(fpath)
            _reported_retrying_spools.discard(fpath)
            continue
        except OSError as exc:
            # A permission, mount, or transient read failure must not destroy the
            # only durable exit record. Stop this pass to preserve queue order.
            logger.warning(
                "[PMS] Could not read forward spool %s; retained for retry: %s",
                fpath,
                exc,
            )
            break

        # Entry V2 authoritative owns all entry evidence. Preserve pre-cutover
        # legacy entry spool files as a quarantine for operator inspection, but
        # never replay them into the legacy VA gallery. Exit payloads remain on
        # their established drain path.
        if (
            settings.ENTRY_V2_MODE == "authoritative"
            and str(record.get("direction") or "").strip().lower() != "exit"
        ):
            if fpath not in _reported_quarantined_spools:
                logger.warning(
                    "[PMS] Quarantined legacy entry spool plate=%s direction=%s "
                    "while Entry V2 is authoritative",
                    record.get("plate"),
                    record.get("direction"),
                )
                _reported_quarantined_spools.add(fpath)
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
        if age is not None and age > max_age and not _is_active_v2_exit(record):
            try:
                await run_in_threadpool(_remove_spool_file, fpath)
                logger.warning(
                    f"[PMS] Dropped stale spooled plate={record.get('plate')} "
                    f"(age {age:.0f}s > {max_age:.0f}s)"
                )
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "[PMS] Could not remove stale forward spool %s: %s",
                    fpath,
                    exc,
                )
            continue

        body = {
            "plate": record.get("plate"),
            "direction": record.get("direction"),
            "image_base64": record.get("image_base64", ""),
        }
        if "captured_at" in record:
            # Preserve the exact source timestamp string from the original
            # delivery.  Replacing it with replay time recreates the delayed
            # exit/re-entry race this spool is meant to survive.
            body["captured_at"] = record.get("captured_at")
        status = await _deliver_anpr_payload(
            body,
            log_retry_failure=fpath not in _reported_retrying_spools,
        )
        if status in ("ok", "drop"):
            # Delivered or permanently rejected — remove and keep draining.
            try:
                await run_in_threadpool(_remove_spool_file, fpath)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "[PMS] Could not remove completed forward spool %s: %s",
                    fpath,
                    exc,
                )
            _reported_retrying_spools.discard(fpath)
            continue
        # "retry" — VA still unreachable; leave the rest for the next interval.
        if fpath not in _reported_retrying_spools:
            logger.warning(
                "[PMS] Spooled delivery still unavailable plate=%s direction=%s; "
                "future identical warnings are suppressed until state changes",
                body.get("plate"),
                body.get("direction"),
            )
            _reported_retrying_spools.add(fpath)
        break
