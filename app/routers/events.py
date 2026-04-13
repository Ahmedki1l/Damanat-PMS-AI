# app/routers/events.py
"""
Camera event webhook endpoint + raw event log viewer.
POST /events/camera — receives events from all cameras (XML or JSON).
GET  /events       — lists raw event log with optional filters.
"""

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from datetime import datetime, UTC
from typing import List, Optional
from app.database import get_db
from app.services.event_parser import parse_camera_event
from app.services.event_dispatcher import dispatch_event
from app.services.occupancy_service import record_event_in_cache
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/events/camera", summary="Camera webhook — receives all events")
async def receive_camera_event(request: Request, db: Session = Depends(get_db)):
    """
    Single entry point for ALL camera events (Phase 1 + Phase 2).
    Always returns HTTP 200 to prevent camera retry loops.
    """
    camera_ip = request.client.host
    content_type = request.headers.get("content-type", "")
    content_length = request.headers.get("content-length", "unknown")
    
    logger.debug(f"Received request from {camera_ip} (CT: {content_type}, CL: {content_length})")

    try:
        raw_body = await request.body()
        if not raw_body:
            logger.warning(f"Ignoring empty body received from {camera_ip}")
            return {"status": "ignored", "detail": "empty body"}

        # 1. Parse unified event
        event = parse_camera_event(raw_body, camera_ip, content_type)
        logger.info(f"Parsed Event: type={event.event_type} | camera={event.camera_id} | plate={event.plate_number}")

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

        return {"status": "ok", "event_type": event.event_type}

    except Exception as e:
        db.rollback()
        logger.error(f"Event processing error: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}
