# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.entry_exit_service import handle_anpr_event
from app.services.camera_feed_service import add_event_to_feed
from app.services.snapshot_service import fetch_snapshot
from app.utils.event_bus import event_bus
from app.utils.logger import get_logger
from app.config import settings
from sqlalchemy.orm import Session

logger = get_logger(__name__)

async def dispatch_event(event: ParsedCameraEvent, db: Session) -> dict:
    """
    Route event to handlers and commit as a single transaction.
    Protects UC3 and UC2 logic while maintaining teammates' UC5/UC6 work.

    Returns a dict of post-commit callbacks. The router must call these
    AFTER db.commit() succeeds (FIX #2: cache-vs-rollback mismatch).
    """
    # Collects cache keys that should only be recorded after successful commit
    _post_commit_cache_keys = []
    # Snapshot strategy:
    # 1. Use multipart image if the camera attached one to the event (already saved locally)
    # 2. If no image → try fetching a fresh snapshot from the camera (saves locally)
    # 3. If camera is unreachable → leave snapshot_path as None
    SNAPSHOT_EVENT_TYPES = (
        "fielddetection", "linedetection", "regionEntrance",
        "AccessControllerEvent", "vehicleMatchResult", "ANPR",
    )
    if event.event_type in SNAPSHOT_EVENT_TYPES:
        # event_parser.parse_camera_event already sets snapshot_path (URL) and
        # local_snapshot_path on multipart events. If neither is set, fall
        # back to fetching a fresh JPEG from the camera here.
        if not event.snapshot_path:
            try:
                fresh = await fetch_snapshot(event.camera_id, event.event_type)
                if fresh:
                    event.snapshot_path, event.local_snapshot_path = fresh
            except Exception as e:
                logger.warning(f"Snapshot fetch failed for {event.camera_id}: {e}")

    try:
        logger.debug(f"[dispatch] type={event.event_type!r} camera={event.camera_id} plate={event.plate_number!r}")

        # VMD = Video Motion Detection: basic motion, not a Smart Event.
        # Ignore it completely per user request — fires constantly and has no zone info.
        if event.event_type == "VMD":
            return

        is_gate = settings.CAMERAS.get(event.camera_id, {}).get("gate") in ("entry", "exit")
        is_occupancy_event = event.event_type == "linedetection"
        should_log = False
    # Selective Logging: Only log gate events or smart events to reduce noise
        include = {x.strip() for x in settings.LOG_CAMERA_FILTER.split(",") if x.strip()} if settings.LOG_CAMERA_FILTER else set()
        exclude = {x.strip() for x in settings.LOG_CAMERA_EXCLUDE.split(",") if x.strip()} if settings.LOG_CAMERA_EXCLUDE else set()

        if include:
            should_log = event.camera_id in include
        elif exclude:
            should_log = event.camera_id not in exclude
        else:
            should_log = True

        if should_log and not is_gate and (is_occupancy_event or event.event_type in ("fielddetection", "linedetection", "regionEntrance")):
            logger.info(f"Event: type={event.event_type} | camera={event.camera_id} | plate={event.plate_number}")
            from app.services.alert_service import broadcast_event
            await broadcast_event(
                is_alert=False,
                severity="info",
                event_type=event.event_type,
                description=f"Live Event from {event.camera_id}",
                camera_id=event.camera_id,
                plate_number=event.plate_number,
                triggered_at=event.trigger_time,
                snapshot_path=event.snapshot_path
            )

        # ── CAMERA FEED ──────────────────────────────────────────────────────
        # Log all entry/exit related smart events to the CameraFeed table.
        FEED_EVENT_TYPES = ("ANPR", "vehicleMatchResult", "AccessControllerEvent", "linedetection")
        if event.event_type in FEED_EVENT_TYPES:
            add_event_to_feed(db, event)

        # ── PHASE 1 ───────────────────────────────────────────────────────────
        
        # ✅ UC3: Parking Occupancy
        # Only line-crossing occupancy cameras are allowed to affect counts.
        is_occupancy_cam = event.camera_id in ("CAM-03", "CAM-08", "CAM-09", "CAM-10")
        is_smart_occupancy_event = is_occupancy_cam

        if is_smart_occupancy_event:
            # FIX #2: handle_occupancy_event now returns a cache_key (or None).
            # We collect it for post-commit recording instead of caching pre-commit.
            oc_cache_key = await handle_occupancy_event(event, db)
            if oc_cache_key:
                _post_commit_cache_keys.append(oc_cache_key)
        elif is_gate:
             logger.debug(f"[UC3] Gate camera {event.camera_id} sent non-occupancy event: {event.event_type}")

        # ── PHASE 2 ───────────────────────────────────────────────────────────
        # UC1 + UC2 + UC4: ANPR gate events (JSON=AccessControllerEvent, XML=ANPR/vehicleMatchResult)
        if event.event_type in ("ANPR", "vehicleMatchResult", "AccessControllerEvent") and event.plate_number:
            await handle_anpr_event(event, db)

        # ── MAINTENANCE ───────────────────────────────────────────────────────
        # Pending logic removed per user request

        # FIX #2: Return pending cache keys for the router to record after commit
        return {"occupancy_cache_keys": _post_commit_cache_keys}

    except Exception as e:
        db.rollback()
        logger.error(f"Dispatch failed, transaction rolled back: {e}", exc_info=True)
        raise