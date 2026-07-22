"""Transient Entry V2 evidence forwarding from PMS-AI to Video Analytics.

The camera still talks only to PMS-AI. This module forwards derived in-memory
vehicle crops without base64 encoding, filesystem persistence, or an internal
retry queue. In authoritative mode a retryable VA result is surfaced to the
camera as HTTP 503 so the source request remains the retry boundary.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
from typing import Optional
from uuid import UUID, uuid5

import httpx
from starlette.concurrency import run_in_threadpool

from app.config import facility_tz, settings
from app.services.event_parser import (
    ParsedCameraEvent,
    TransientImage,
    crossing_matches_configured_entry_filter,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

_ID_NAMESPACE = UUID("3a5c6c63-940e-49d3-a397-8cbd2485f105")
_ATTEMPT_TYPES = {"ANPR", "AccessControllerEvent"}
_CROSSING_ROLES = {"CAM-23": "primary", "CAM-03": "fallback"}
_ALIAS_TARGETS = {"CAM-ENTRY", "CAM-EXIT"}
_MODE_HEADER = "X-Entry-V2-Mode"
_http_client: Optional[httpx.AsyncClient] = None


def _vehicle_images(event: ParsedCameraEvent) -> tuple[TransientImage, ...]:
    """Defense in depth: outbound V2 accepts derived vehicle crops only."""
    return tuple(
        image for image in event.transient_images if image.role == "vehicle"
    )[: settings.ENTRY_V2_MAX_IMAGES]


class ForwardOutcome(str, Enum):
    ACCEPTED = "accepted"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ForwardResult:
    outcome: ForwardOutcome
    evidence_id: str
    status_code: Optional[int] = None
    detail: str = ""
    retry_after: Optional[str] = None

    @property
    def retryable(self) -> bool:
        return self.outcome is ForwardOutcome.UNAVAILABLE


def _semantic_ack_error(
    response: httpx.Response,
    evidence_id: str,
    expected_mode: str,
) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "VA response is not valid JSON"
    if not isinstance(payload, dict):
        return "VA response JSON must be an object"
    if payload.get("mode") != expected_mode:
        return (
            f"VA response mode mismatch: expected {expected_mode}, received "
            f"{payload.get('mode') or 'missing'}"
        )
    if payload.get("id") != evidence_id:
        return "VA response id does not match submitted evidence"
    response_status = payload.get("status")
    if response_status not in {"accepted", "duplicate"}:
        return "VA response status is not an ingest acknowledgement"
    expected_http_status = 200 if response_status == "duplicate" else 201
    if response.status_code != expected_http_status:
        return (
            f"VA HTTP {response.status_code} conflicts with "
            f"{response_status} acknowledgement"
        )
    duplicate = payload.get("duplicate")
    if not isinstance(duplicate, bool) or duplicate != (
        response_status == "duplicate"
    ):
        return "VA response duplicate flag conflicts with status"
    return ""


def is_authoritative() -> bool:
    return settings.ENTRY_V2_MODE == "authoritative"


def resolve_entry_v2_camera_alias(event: ParsedCameraEvent) -> bool:
    """Resolve an otherwise-unknown event from an explicit trusted alias."""
    current_gate = settings.CAMERAS.get(event.camera_id, {}).get("gate", event.gate)
    if current_gate in {"entry", "exit"}:
        return False

    sources = {str(event.camera_id).strip(), str(event.device_serial).strip()}
    matches: set[str] = set()
    for item in settings.ENTRY_V2_CAMERA_ALIASES.split(","):
        source, separator, target = item.strip().partition("=")
        if separator and source.strip() in sources and target.strip():
            matches.add(target.strip())

    if not matches:
        return False
    if len(matches) != 1:
        logger.error(
            "[EntryV2] Conflicting camera aliases for camera=%s serial=%s: %s",
            event.camera_id,
            event.device_serial,
            sorted(matches),
        )
        return False

    target = next(iter(matches))
    target_config = settings.CAMERAS.get(target, {})
    target_gate = target_config.get("gate")
    if target not in _ALIAS_TARGETS or target_gate not in {"entry", "exit"}:
        logger.error(
            "[EntryV2] Refusing camera alias target=%s; it must be a configured "
            "CAM-ENTRY/CAM-EXIT gate",
            target,
        )
        return False

    logger.info(
        "[EntryV2] Resolved camera alias %s/%s -> %s (gate=%s)",
        event.camera_id,
        event.device_serial,
        target,
        target_gate,
    )
    event.camera_id = target
    event.gate = target_gate
    return True


def is_entry_attempt(event: ParsedCameraEvent) -> bool:
    """Classify evidence-bearing entry ANPR events.

    Entry must be positively resolved. The deployed entry ANPR can initially
    appear as ``UNKNOWN-<gateway-ip>``, so the router first applies an exact
    same-plate VMR hint. Any still-unresolved event fails closed instead of
    risking that an unknown exit is uploaded as an entry attempt.
    """
    if event.event_type not in _ATTEMPT_TYPES:
        return False
    gate = settings.CAMERAS.get(event.camera_id, {}).get("gate", event.gate)
    return gate == "entry"


def is_unresolved_attempt(event: ParsedCameraEvent) -> bool:
    if event.event_type not in _ATTEMPT_TYPES:
        return False
    gate = settings.CAMERAS.get(event.camera_id, {}).get("gate", event.gate)
    return gate not in {"entry", "exit"}


def is_non_exit_attempt(event: ParsedCameraEvent) -> bool:
    """Entry or unresolved ANPR boundary event suppressed from legacy writes."""
    return is_entry_attempt(event) or is_unresolved_attempt(event)


def is_entry_crossing(event: ParsedCameraEvent) -> bool:
    return (
        event.event_type == "linedetection"
        and event.camera_id in _CROSSING_ROLES
        and str(event.event_state or "").strip().lower() == "active"
        and str(event.detection_target or "").strip().lower() == "vehicle"
        and crossing_matches_configured_entry_filter(event)
    )


def is_entry_v2_evidence(event: ParsedCameraEvent) -> bool:
    return is_entry_attempt(event) or is_entry_crossing(event)


def _configured_crossing_direction(camera_id: str) -> str:
    for item in settings.ENTRY_CONFIRM_DIRECTIONS.split(","):
        camera, separator, direction = item.strip().partition(":")
        if separator and camera.strip() == camera_id and direction.strip():
            return direction.strip()
    return ""


def _is_configured_one_way_line(camera_id: str, line_id: Optional[str]) -> bool:
    expected_line = str(line_id or "").strip()
    if not expected_line:
        return False
    for item in settings.ENTRY_V2_ONE_WAY_LINES.split(","):
        camera, separator, configured_line = item.strip().partition(":")
        if (
            separator
            and camera.strip() == camera_id
            and configured_line.strip() == expected_line
        ):
            return True
    return False


def _effective_crossing_direction(event: ParsedCameraEvent) -> tuple[str, str]:
    raw_direction = str(event.crossing_direction or "").strip()
    if raw_direction:
        return raw_direction, "camera"
    if _is_configured_one_way_line(event.camera_id, event.region_id):
        configured = _configured_crossing_direction(event.camera_id)
        if configured:
            return configured, "configured_one_way_line"
    return "", "missing"


def _metadata(
    event: ParsedCameraEvent,
    images: Optional[tuple[TransientImage, ...]] = None,
) -> dict:
    if images is None:
        images = _vehicle_images(event)
    effective_direction, direction_source = _effective_crossing_direction(event)
    return {
        "event_type": event.event_type,
        "device_serial": event.device_serial,
        "channel_id": event.channel_id,
        "channel_name": event.channel_name,
        "event_state": event.event_state,
        "event_description": event.event_description,
        "timestamp_source": event.trigger_time_source,
        "pic_num": event.pic_num,
        "raw_line_id": event.region_id,
        "raw_direction": event.crossing_direction,
        "effective_direction": effective_direction,
        "direction_source": direction_source,
        "image_roles": [image.role for image in images],
        "raw_event_sha256": hashlib.sha256(
            event.raw_xml.encode("utf-8", errors="replace")
        ).hexdigest(),
        "images": [
            {
                "index": index,
                "role": image.role,
                "filename": image.filename,
                "content_type": image.content_type,
                "size_bytes": len(image.data),
                "sha256": hashlib.sha256(image.data).hexdigest(),
            }
            for index, image in enumerate(images)
        ],
    }


def _source_event_id(
    event: ParsedCameraEvent,
    *,
    metadata: Optional[dict] = None,
) -> str:
    if metadata is None:
        metadata = _metadata(event)
    digest = hashlib.sha256()
    stable_fields = {
        "camera_id": event.camera_id,
        "event_type": event.event_type,
        "reported_plate": event.plate_number,
        "reported_confidence": event.plate_confidence,
        "line_id": event.region_id,
        "direction": _effective_crossing_direction(event)[0],
        "metadata": metadata,
    }
    if not event.trigger_time_source.startswith("pms_receive_"):
        stable_fields["captured_at"] = _aware_trigger_time(event).isoformat()
    digest.update(
        json.dumps(stable_fields, sort_keys=True, separators=(",", ":")).encode()
    )
    # Metadata already contains each image digest. Reuse it instead of hashing
    # the same multi-megabyte buffers a second time.
    for descriptor in metadata["images"]:
        digest.update(bytes.fromhex(descriptor["sha256"]))
    return str(uuid5(_ID_NAMESPACE, f"source:{digest.hexdigest()}"))


def _request_parts(
    event: ParsedCameraEvent,
) -> tuple[str, dict, list[tuple[str, tuple[str, bytes, str]]]]:
    images = _vehicle_images(event)
    metadata = _metadata(event, images)
    source_event_id = _source_event_id(
        event,
        metadata=metadata,
    )
    captured_at = _aware_trigger_time(event).isoformat()
    metadata_json = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
    if is_entry_attempt(event):
        evidence_id = str(uuid5(_ID_NAMESPACE, f"attempt:{source_event_id}"))
        data = {
            "attempt_id": evidence_id,
            "source_event_id": source_event_id,
            "camera_id": event.camera_id,
            "captured_at": captured_at,
            "reported_plate": event.plate_number or "",
            "metadata_json": metadata_json,
        }
        if event.plate_confidence is not None:
            data["reported_confidence"] = str(event.plate_confidence)
        path = "/api/v2/entry-attempts"
    else:
        effective_direction, _ = _effective_crossing_direction(event)
        evidence_id = str(uuid5(_ID_NAMESPACE, f"crossing:{source_event_id}"))
        data = {
            "crossing_id": evidence_id,
            "source_event_id": source_event_id,
            "camera_id": event.camera_id,
            "captured_at": captured_at,
            "line_id": event.region_id or "",
            "direction": effective_direction,
            "role": _CROSSING_ROLES[event.camera_id],
            "metadata_json": metadata_json,
        }
        path = "/api/v2/entry-crossings"

    files = [
        (
            "images",
            (image.filename, image.data, image.content_type),
        )
        for image in images
    ]
    return path, data, files


def _aware_trigger_time(event: ParsedCameraEvent) -> datetime:
    """Normalize legacy hand-built events to the facility timezone at the V2 edge."""
    captured_at = event.trigger_time
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        return captured_at.replace(tzinfo=facility_tz())
    return captured_at


def _http_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.ENTRY_V2_CONNECT_TIMEOUT_SECONDS,
        read=settings.ENTRY_V2_READ_TIMEOUT_SECONDS,
        write=settings.ENTRY_V2_WRITE_TIMEOUT_SECONDS,
        pool=settings.ENTRY_V2_POOL_TIMEOUT_SECONDS,
    )


async def start_entry_v2_http_client() -> None:
    """Create the app-lifetime VA client once when Entry V2 is active."""
    global _http_client
    if settings.ENTRY_V2_MODE == "off" or _http_client is not None:
        return
    _http_client = httpx.AsyncClient(timeout=_http_timeout())


async def close_entry_v2_http_client() -> None:
    """Close and detach the app-lifetime VA client."""
    global _http_client
    client = _http_client
    _http_client = None
    if client is not None:
        await client.aclose()


async def _post_entry_v2(
    url: str,
    *,
    data: dict,
    files: list[tuple[str, tuple[str, bytes, str]]],
    headers: dict[str, str],
) -> httpx.Response:
    if _http_client is not None:
        return await _http_client.post(
            url,
            data=data,
            files=files,
            headers=headers,
        )
    # Direct service tests and scripts do not run the FastAPI lifecycle. Keep a
    # scoped fallback so they remain correct without leaking a client.
    async with httpx.AsyncClient(timeout=_http_timeout()) as client:
        return await client.post(
            url,
            data=data,
            files=files,
            headers=headers,
        )


async def forward_entry_v2_event(
    event: ParsedCameraEvent,
) -> Optional[ForwardResult]:
    """Forward one relevant event, returning a typed delivery decision."""
    if settings.ENTRY_V2_MODE == "off":
        return None

    if is_unresolved_attempt(event):
        source_event_id = await run_in_threadpool(_source_event_id, event)
        evidence_id = str(uuid5(_ID_NAMESPACE, f"attempt:{source_event_id}"))
        detail = "entry gate could not be resolved"
        logger.warning(
            "[EntryV2] Rejecting unresolved ANPR evidence=%s camera=%s",
            evidence_id,
            event.camera_id,
        )
        return ForwardResult(
            ForwardOutcome.UNAVAILABLE,
            evidence_id=evidence_id,
            detail=detail,
        )

    if not is_entry_v2_evidence(event):
        return None

    path, data, files = await run_in_threadpool(_request_parts, event)
    evidence_id = data.get("attempt_id") or data["crossing_id"]

    if not files:
        detail = "no valid vehicle crop"
        logger.warning(
            "[EntryV2] Rejecting evidence=%s before delivery: %s",
            evidence_id,
            detail,
        )
        return ForwardResult(
            ForwardOutcome.INVALID,
            evidence_id=evidence_id,
            status_code=422,
            detail=detail,
        )

    if is_entry_crossing(event) and not data["direction"]:
        detail = "crossing direction requires camera data or one-way line policy"
        logger.warning(
            "[EntryV2] Rejecting crossing=%s before delivery: %s",
            evidence_id,
            detail,
        )
        return ForwardResult(
            ForwardOutcome.INVALID,
            evidence_id=evidence_id,
            status_code=422,
            detail=detail,
        )

    if not settings.PMS_API_URL or not settings.ENTRY_V2_SERVICE_KEY:
        detail = "ENTRY_V2 requires PMS_API_URL and ENTRY_V2_SERVICE_KEY"
        logger.error("[EntryV2] %s", detail)
        return ForwardResult(
            ForwardOutcome.UNAVAILABLE,
            evidence_id=evidence_id,
            detail=detail,
        )

    try:
        response = await _post_entry_v2(
            f"{settings.PMS_API_URL.rstrip('/')}{path}",
            data=data,
            files=files,
            headers={
                "X-Service-Key": settings.ENTRY_V2_SERVICE_KEY,
                _MODE_HEADER: settings.ENTRY_V2_MODE,
            },
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning(
            "[EntryV2] VA unavailable for evidence=%s: %s",
            evidence_id,
            exc,
        )
        return ForwardResult(
            ForwardOutcome.UNAVAILABLE,
            evidence_id=evidence_id,
            detail=type(exc).__name__,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "[EntryV2] HTTP failure for evidence=%s: %s",
            evidence_id,
            exc,
        )
        return ForwardResult(
            ForwardOutcome.UNAVAILABLE,
            evidence_id=evidence_id,
            detail=type(exc).__name__,
        )

    detail = response.text[:300]
    if 200 <= response.status_code < 300:
        ack_error = _semantic_ack_error(
            response,
            evidence_id,
            settings.ENTRY_V2_MODE,
        )
        if ack_error:
            logger.error("[EntryV2] %s evidence=%s", ack_error, evidence_id)
            return ForwardResult(
                ForwardOutcome.UNAVAILABLE,
                evidence_id=evidence_id,
                status_code=response.status_code,
                detail=ack_error,
            )
        logger.info(
            "[EntryV2] VA accepted evidence=%s status=%s",
            evidence_id,
            response.status_code,
        )
        return ForwardResult(
            ForwardOutcome.ACCEPTED,
            evidence_id=evidence_id,
            status_code=response.status_code,
            detail=detail,
        )
    retryable_statuses = {401, 403, 404, 405, 408, 425, 429, 503}
    if (
        300 <= response.status_code < 400
        or response.status_code in retryable_statuses
        or response.status_code >= 500
    ):
        logger.warning(
            "[EntryV2] VA backpressure evidence=%s status=%s: %s",
            evidence_id,
            response.status_code,
            detail,
        )
        return ForwardResult(
            ForwardOutcome.UNAVAILABLE,
            evidence_id=evidence_id,
            status_code=response.status_code,
            detail=detail,
            retry_after=response.headers.get("Retry-After"),
        )

    logger.warning(
        "[EntryV2] VA rejected evidence=%s status=%s: %s",
        evidence_id,
        response.status_code,
        detail,
    )
    return ForwardResult(
        ForwardOutcome.INVALID,
        evidence_id=evidence_id,
        status_code=response.status_code,
        detail=detail,
    )
