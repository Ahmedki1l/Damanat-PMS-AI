# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.violation_service import handle_violation_event, resolve_violation_on_exit, RESTRICTED_ZONES
from app.zone_config import resolve_zone
from app.services.intrusion_service import handle_intrusion_event, MONITORED_INTRUSION_ZONES
from app.services.entry_exit_service import handle_anpr_event
from app.services.snapshot_service import fetch_snapshot
from app.utils.logger import get_logger
from app.config import settings
from sqlalchemy.orm import Session

logger = get_logger(__name__)

async def dispatch_event(event: ParsedCameraEvent, db: Session):
    """
    Route event to handlers and commit as a single transaction.
    Protects UC3 and UC2 logic while maintaining teammates' UC5/UC6 work.
    """
    # Snapshot strategy (priority order):
    # 1. If multipart detection frame was saved locally → upload to Spaces → CDN URL
    # 2. If no multipart image → try fetching fresh snapshot from camera
    # 3. If camera is unreachable too → leave snapshot_path as None
    SNAPSHOT_EVENT_TYPES = (
        "fielddetection", "linedetection", "regionEntrance",
        "AccessControllerEvent", "vehicleMatchResult", "ANPR",
    )
    if event.event_type in SNAPSHOT_EVENT_TYPES:
        # Step 1: Upload the multipart detection frame (the actual evidence) to Spaces
        if (
            event.snapshot_path
            and settings.STORAGE_MODE == "spaces"
            and not event.snapshot_path.startswith("http")
        ):
            try:
                import os
                from app.utils.spaces_client import upload_image
                local_path = event.snapshot_path
                if os.path.exists(local_path):
                    with open(local_path, "rb") as _f:
                        _image_bytes = _f.read()
                    _filename = os.path.basename(local_path)
                    cdn_url = upload_image(_image_bytes, _filename)
                    if cdn_url:
                        event.snapshot_path = cdn_url
                        logger.info(f"[Spaces] Multipart image uploaded: {cdn_url}")
                    else:
                        event.snapshot_path = None  # don't store local path in DB
                else:
                    event.snapshot_path = None
            except Exception as e:
                logger.warning(f"Multipart upload to Spaces failed for {event.camera_id}: {e}")
                event.snapshot_path = None

        # Step 2: No CDN URL yet — try fetching a fresh snapshot from the camera
        if not event.snapshot_path or not event.snapshot_path.startswith("http"):
            try:
                fresh = await fetch_snapshot(event.camera_id, event.event_type)
                if fresh:
                    event.snapshot_path = fresh
            except Exception as e:
                logger.warning(f"Snapshot fetch failed for {event.camera_id}: {e}")

    try:
        logger.debug(f"[dispatch] type={event.event_type!r} camera={event.camera_id} plate={event.plate_number!r}")

        # VMD = Video Motion Detection: basic motion, not a Smart Event.
        # Ignore it completely per user request — fires constantly and has no zone info.
        if event.event_type == "VMD":
            return

        is_gate = settings.CAMERAS.get(event.camera_id, {}).get("gate") in ("entry", "exit")
        is_occupancy_event = event.event_type in ("ANPR", "vehicleMatchResult", "AccessControllerEvent")
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

        if should_log and (is_gate or is_occupancy_event or event.event_type in ("fielddetection", "linedetection", "regionEntrance")):
            logger.info(f"Event: type={event.event_type} | camera={event.camera_id} | plate={event.plate_number}")


        is_vehicle = event.detection_target in ("vehicle", None)
        is_human = event.detection_target in ("human", None)
        resolved_zone = resolve_zone(event.camera_id, event.region_id)
        zone_id = event.region_id or ""
        # Auto-resolve violations on exit from restricted zones
        if event.event_type in ("regionExiting", "regionExit") and is_vehicle:
            canonical_zone = resolved_zone or zone_id
            if canonical_zone in RESTRICTED_ZONES:
                await resolve_violation_on_exit(event.camera_id, canonical_zone, db)
        # ── PHASE 1 ───────────────────────────────────────────────────────────
        
        # ✅ UC3: Parking Occupancy
        # Strictly using ANPR gate systems (Entry/Exit) OR internal transition gates (CAM-03/08/09/10)
        is_occupancy_cam = event.camera_id in ("CAM-03", "CAM-08", "CAM-09", "CAM-10")
        is_smart_occupancy_event = is_occupancy_event or (event.event_type == "linedetection" and is_occupancy_cam)

        if is_smart_occupancy_event or is_occupancy_cam:
            if is_gate or is_occupancy_cam:
                await handle_occupancy_event(event, db)
            else:
                logger.debug(f"[UC3] Ignoring occupancy event from {event.camera_id} (not a configured gate)")
        elif is_gate:
             # If it's a gate camera but not an ANPR event (e.g. VMD, duration), we might want to know
             logger.debug(f"[UC3] Gate camera {event.camera_id} sent non-vehicle event: {event.event_type}")


        # ✅ UC5 & UC6: Violations and Intrusion (Teammates' Tasks)
        # Logic: Route based on zone lists to prevent double-firing
        if event.event_type in ("fielddetection", "regionEntrance") and is_vehicle:
            route_zone = resolved_zone or zone_id
            if route_zone in MONITORED_INTRUSION_ZONES:
                await handle_intrusion_event(event, db)
            elif route_zone in RESTRICTED_ZONES:
                await handle_violation_event(event, db)
            elif not event.region_id:
                # Fallback if no zone name: fire both, services will self-filter
                await handle_violation_event(event, db)
                await handle_intrusion_event(event, db)

        # Line Crossing: violation unless it's a designated occupancy gate
        if event.event_type == "linedetection" and (is_vehicle or is_human):
            is_occupancy_gate = (
                zone_id in settings.OCCUPANCY_ENTRANCE_ZONES or 
                zone_id in settings.OCCUPANCY_EXIT_ZONES
            )
            if not is_occupancy_gate:
                await handle_violation_event(event, db)

        # ── PHASE 2 ───────────────────────────────────────────────────────────
        # UC1 + UC2 + UC4: ANPR gate events (JSON=AccessControllerEvent, XML=ANPR/vehicleMatchResult)
        if event.event_type in ("AccessControllerEvent", "ANPR", "vehicleMatchResult") and event.plate_number:
            await handle_anpr_event(event, db)

        # ── MAINTENANCE ───────────────────────────────────────────────────────
        # Finalize any pending exits that have passed the confirmation window
        from app.services.occupancy_service import process_pending_exits
        await process_pending_exits(db)

    except Exception as e:
        db.rollback()
        logger.error(f"Dispatch failed, transaction rolled back: {e}", exc_info=True)
        raise