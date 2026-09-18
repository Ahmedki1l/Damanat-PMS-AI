# app/routers/events.py
"""
Camera event webhook endpoint + raw event log viewer.
POST /events/camera — receives events from all cameras (XML or JSON).
GET  /events       — lists raw event log with optional filters.
"""

from dataclasses import dataclass, replace
from ipaddress import ip_address
from typing import Optional

from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from app.config import parse_camera_source_networks, settings
from app.database import get_db
from app.services.event_parser import (
    CameraPayloadRejected,
    finalize_camera_event_images,
    parse_camera_event,
)
from app.services.event_dispatcher import dispatch_event, resolve_exact_vmr_identity
from app.services.entry_v2_forwarder import (
    enqueue_entry_v2_shadow,
    forward_entry_v2_event,
    is_authoritative as entry_v2_is_authoritative,
    resolve_entry_v2_camera_alias,
)
from app.services.entry_state_lock import EntryStateLockUnavailable
from app.services.entry_exit_service import (
    SourceTimestampUnavailable,
    note_gate_event,
)
from app.services.camera_ingest_spool import spool_camera_event
from app.services.occupancy_service import record_event_in_cache
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@dataclass
class CameraEventOutcome:
    """What processing one camera event concluded, independent of HTTP.

    The live webhook turns this into a response; the spool drainer uses it to
    decide whether a record is drained, retried or quarantined. Keeping the two
    consumers on one vocabulary is what stops a replay from re-deriving that
    decision by parsing status codes.
    """

    status: str  # "ok" | "rejected" | "error" | "retry"
    detail: str = ""
    evidence_id: Optional[str] = None
    retry_after: Optional[str] = None
    event_type: Optional[str] = None

    @property
    def retryable(self) -> bool:
        return self.status == "retry"


async def _outcome_to_camera_response(
    outcome: CameraEventOutcome,
    raw_body: bytes,
    camera_ip: str,
    content_type: str,
):
    """Answer the camera, spooling anything retryable instead of discarding it.

    A camera-facing 503 is not a deferral. Hikvision push ignores Retry-After and
    never re-POSTs — 332 of 332 picture IDs in a production week were distinct, so
    nothing was ever re-delivered. Every 503 therefore deleted the event. When the
    spool is enabled we persist the request and acknowledge 200; the drainer owns
    the retry from there. If the spool refuses (full volume, unwritable path) we
    fall back to the old 503, which is worse but honest.
    """
    if not outcome.retryable:
        payload = {"status": outcome.status}
        if outcome.detail:
            payload["detail"] = outcome.detail
        if outcome.event_type:
            payload["event_type"] = outcome.event_type
        return payload

    # scandir + a write of up to 1.2 MB + fsync. This runs for EVERY camera event
    # during a downstream outage, and PMS-AI serves all cameras from one event
    # loop with a synchronous pyodbc driver, so doing it inline would stall every
    # concurrent camera handler.
    spooled = settings.CAMERA_INGEST_SPOOL_ENABLED and await run_in_threadpool(
        spool_camera_event,
        raw_body,
        camera_ip,
        content_type,
        outcome.detail or "retryable",
        evidence_id=outcome.evidence_id,
    )
    if spooled:
        logger.info(
            "[IngestSpool] acknowledged camera event that would have been a 503 "
            "(reason=%s evidence=%s) — queued for replay",
            outcome.detail,
            outcome.evidence_id,
        )
        return {"status": "accepted", "detail": "queued for processing"}

    logger.warning(
        "[EntryV2] Returning camera-facing 503 for evidence=%s — THIS EVENT IS LOST, "
        "the camera will not retry it",
        outcome.evidence_id,
    )
    content = {"status": "retry", "detail": outcome.detail or "unavailable"}
    if outcome.evidence_id:
        content["evidence_id"] = outcome.evidence_id
    return JSONResponse(
        status_code=503,
        content=content,
        headers={"Retry-After": outcome.retry_after or "1"},
    )


class CameraBodyTooLarge(Exception):
    """The camera payload crossed the configured streaming byte limit."""


def _camera_source_is_allowed(source_ip: str) -> bool:
    """Match the effective connection peer against the optional CIDR allowlist."""
    try:
        networks = parse_camera_source_networks(
            settings.CAMERA_EVENT_ALLOWED_SOURCE_CIDRS
        )
    except ValueError:
        logger.error(
            "Invalid CAMERA_EVENT_ALLOWED_SOURCE_CIDRS; camera webhook "
            "fails closed"
        )
        return False
    if not networks:
        return settings.ENTRY_V2_MODE != "authoritative"

    try:
        source = ip_address(source_ip)
    except ValueError:
        logger.warning("Rejected camera event with invalid peer IP: %r", source_ip)
        return False
    mapped_ipv4 = getattr(source, "ipv4_mapped", None)
    if mapped_ipv4 is not None:
        source = mapped_ipv4

    return any(source.version == network.version and source in network for network in networks)


async def _read_camera_body_limited(request: Request, limit: int) -> bytes:
    """Read a fixed or chunked body without ever buffering more than ``limit``."""
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            raise CameraBodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def _camera_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"status": "error", "detail": detail},
    )


@router.post("/events/camera", summary="Camera webhook — receives all events")
async def receive_camera_event(request: Request, db: Session = Depends(get_db)):
    """
    Single entry point for ALL camera events (Phase 1 + Phase 2).
    Legacy/shadow processing acknowledges camera events with HTTP 200.
    Authoritative Entry V2 returns HTTP 503 only for retryable VA failures.
    """
    camera_ip = request.client.host if request.client else ""
    content_type = request.headers.get("content-type", "")
    content_length = request.headers.get("content-length", "unknown")

    if not _camera_source_is_allowed(camera_ip):
        logger.warning("Rejected camera webhook from untrusted source %r", camera_ip)
        return _camera_error(403, "camera source is not allowed")

    max_body_bytes = settings.CAMERA_EVENT_MAX_BODY_BYTES
    if max_body_bytes <= 0:
        logger.error("CAMERA_EVENT_MAX_BODY_BYTES must be greater than zero")
        return _camera_error(503, "camera webhook size limit is not configured")
    if content_length != "unknown":
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            return _camera_error(400, "invalid Content-Length")
        if declared_length < 0:
            return _camera_error(400, "invalid Content-Length")
        if declared_length > max_body_bytes:
            return _camera_error(413, "camera event payload is too large")
    
    logger.debug(f"Received request from {camera_ip} (CT: {content_type}, CL: {content_length})")

    try:
        raw_body = await _read_camera_body_limited(request, max_body_bytes)
    except CameraBodyTooLarge:
        return _camera_error(413, "camera event payload is too large")
    except Exception:
        # A truncated POST or a client disconnect is the camera's problem, not a
        # server fault. Before the handler was split this fell into the generic
        # handler below; letting it escape now would surface as a 500.
        logger.warning("Camera body read failed for %s", camera_ip, exc_info=True)
        return {"status": "error", "detail": "camera body read failed"}
    if not raw_body:
        logger.warning(f"Ignoring empty body received from {camera_ip}")
        return {"status": "ignored", "detail": "empty body"}

    outcome = await process_camera_event(raw_body, camera_ip, content_type, db)
    return await _outcome_to_camera_response(outcome, raw_body, camera_ip, content_type)


async def process_camera_event(
    raw_body: bytes,
    camera_ip: str,
    content_type: str,
    db: Session,
) -> CameraEventOutcome:
    """Process one camera event. Shared by the live webhook and the spool drainer.

    Returns an outcome rather than an HTTP response so the drainer can decide
    whether a record is drained, retried or quarantined without having to
    reverse-engineer that decision from a status code.
    """
    try:
        # 1. Parse unified event
        event = await run_in_threadpool(
            parse_camera_event,
            raw_body,
            camera_ip,
            content_type,
        )
        logger.info(f"Parsed Event: type={event.event_type} | camera={event.camera_id} | plate={event.plate_number}")

        # Resolve an explicit source alias first, then only a same-plate VMR
        # hint before forwarding. These are camera-identity signals PMS-AI
        # already trusts; neither changes the reported plate.
        resolve_entry_v2_camera_alias(event)
        # The exact VMR hint is another camera identity PMS-AI already trusts;
        # the legacy plate-independent FIFO correction remains later in
        # dispatch and is disabled entirely when V2 is authoritative.
        resolve_exact_vmr_identity(event)
        await run_in_threadpool(finalize_camera_event_images, event)

        # Capture the mode once so one camera request cannot cross execution
        # policies if deployment configuration changes during a rollout.
        entry_v2_mode = settings.ENTRY_V2_MODE
        # Legacy dispatch may apply its plate-independent FIFO rescue in
        # off/shadow and mutate camera_id/gate/plate_number. Shadow must observe
        # the same pre-legacy evidence authoritative mode would receive, while
        # retaining the derived in-memory crops without duplicating their bytes.
        shadow_v2_event = replace(event) if entry_v2_mode == "shadow" else None

        # Authoritative V2 must remain the camera-facing retry boundary and run
        # before any legacy transaction. Shadow is intentionally deferred until
        # after legacy commit below so VA latency can never consume the legacy
        # ANPR/crossing correlation window.
        if entry_v2_mode == "authoritative":
            try:
                v2_result = await forward_entry_v2_event(event)
            except Exception:
                logger.error("[EntryV2] Unexpected forwarding failure", exc_info=True)
                return CameraEventOutcome(
                    status="retry", detail="entry validation unavailable"
                )

            if v2_result is not None and v2_result.retryable:
                logger.warning(
                    "[EntryV2] Returning camera-facing 503 for evidence=%s",
                    v2_result.evidence_id,
                )
                return CameraEventOutcome(
                    status="retry",
                    detail="entry validation unavailable",
                    evidence_id=v2_result.evidence_id,
                    retry_after=v2_result.retry_after,
                )

        # CAM-04 diagnostic: log every field so we can see exit events and ignored types
        if event.camera_id == "CAM-04":
            logger.info(
                f"[CAM-04 DEBUG] type={event.event_type} | state={event.event_state} | "
                f"target={event.detection_target} | region_id={event.region_id} | "
                f"direction={event.crossing_direction} | description={event.event_description}"
            )

        # 2. Dispatch to handlers (UC1–UC6) — this also fetches the snapshot
        #    and sets event.snapshot_path before we persist records.
        #    FIX #2: dispatch now returns pending cache keys for post-commit recording.
        dispatch_result = await dispatch_event(event, db) or {}

        # (Removed unused CameraEvent insertion map here)
        
        # 4. Commit — router owns the transaction, not the dispatcher
        db.commit()

        # FIX #2 (cache-vs-rollback): only record occupancy events in the dedup
        # cache AFTER commit succeeds. If commit had failed (rollback above),
        # the cache stays clean so camera retries are accepted, not dropped.
        for cache_key in dispatch_result.get("occupancy_cache_keys", []):
            record_event_in_cache(cache_key)

        # Exit notifications perform network I/O. Dispatch builds them while
        # mutating the DB, but delivery starts only after commit has released
        # the normalized-plate SQL Server application lock.
        for forward in dispatch_result.get("anpr_forwards", []):
            await forward.deliver()

        # A gate-area event is the heartbeat that sweeps HikCentral for cars the
        # edge pipeline missed entirely. Debounced + fire-and-forget: it spawns a
        # background sweep (own DB session) and never blocks this acknowledgement.
        note_gate_event(event.camera_id)

        # Shadow is observation-only: legacy state and all transaction-owned
        # post-commit work are complete before the bounded FIFO takes ownership
        # of the immutable image bytes. This call never waits on VA network I/O,
        # so VA latency cannot extend the legacy camera acknowledgement path.
        if shadow_v2_event is not None:
            enqueue_entry_v2_shadow(shadow_v2_event)

        return CameraEventOutcome(status="ok", event_type=event.event_type)

    except CameraPayloadRejected as exc:
        logger.warning(
            "Rejected malformed camera payload from %s: %s",
            camera_ip,
            exc,
        )
        return CameraEventOutcome(status="rejected", detail="malformed camera payload")
    except EntryStateLockUnavailable:
        db.rollback()
        logger.warning(
            "Entry/exit state lock is unavailable",
            exc_info=True,
        )
        if entry_v2_is_authoritative():
            return CameraEventOutcome(status="retry", detail="entry state is busy")
        return CameraEventOutcome(status="error", detail="entry state is busy")
    except SourceTimestampUnavailable:
        db.rollback()
        logger.error("Exit event is missing a trustworthy camera timestamp")
        if entry_v2_is_authoritative():
            return CameraEventOutcome(status="retry", detail="exit source time unavailable")
        return CameraEventOutcome(status="error", detail="exit source time unavailable")
    except Exception as e:
        db.rollback()
        logger.error(f"Event processing error: {e}", exc_info=True)
        if entry_v2_is_authoritative():
            return CameraEventOutcome(
                status="retry", detail="camera event processing unavailable"
            )
        return CameraEventOutcome(status="error", detail=str(e))
