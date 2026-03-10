# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.violation_service import handle_violation_event, RESTRICTED_ZONES
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
    try:
        # VMD = Video Motion Detection: basic motion, not a Smart Event.
        # Ignore it completely per user request — fires constantly and has no zone info.
        if event.event_type == "VMD":
            return

        is_gate = settings.CAMERAS.get(event.camera_id, {}).get("gate") in ("entry", "exit")
        is_occupancy_event = event.event_type in ("ANPR", "vehicleMatchResult", "AccessControllerEvent")

        # Selective Logging: Only log gate events or smart events to reduce noise
        if is_gate or is_occupancy_event or event.event_type in ("fielddetection", "linedetection", "regionEntrance"):
            logger.info(f"Event: type={event.event_type} | camera={event.camera_id} | plate={event.plate_number}")

        is_vehicle = event.detection_target in ("vehicle", None)
        is_human = event.detection_target in ("human", None)
        zone_id = event.region_id or ""

        # ── PHASE 1 ───────────────────────────────────────────────────────────
        
        # ✅ UC3: Parking Occupancy
        # Strictly using ANPR gate systems (Entry/Exit) OR internal transition gates (CAM-03/08/09/10)
        is_internal_transition = event.camera_id in ("CAM-03", "CAM-08", "CAM-09", "CAM-10")
        if is_occupancy_event or is_internal_transition:
            if is_gate or is_internal_transition:
                await handle_occupancy_event(event, db)
            else:
                logger.debug(f"[UC3] Ignoring occupancy event from {event.camera_id} (not a configured gate)")
        elif is_gate:
             # If it's a gate camera but not an ANPR event (e.g. VMD, duration), we might want to know
             logger.debug(f"[UC3] Gate camera {event.camera_id} sent non-vehicle event: {event.event_type}")


        # ✅ UC5 & UC6: Violations and Intrusion (Teammates' Tasks)
        # Logic: Route based on zone lists to prevent double-firing
        if event.event_type in ("fielddetection", "regionEntrance") and is_vehicle:
            if zone_id in MONITORED_INTRUSION_ZONES:
                await handle_intrusion_event(event, db)
            elif zone_id in RESTRICTED_ZONES:
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
        
        # ✅ UC1 + UC2 + UC4: ANPR gate events (Your UC2 is here)
        # Process ANPR-specific logic (logging, unregistered alerts)
        if event.event_type in ("AccessControllerEvent", "ANPR", "vehicleMatchResult") and event.plate_number:
            await handle_anpr_event(event, db)

        # ── MAINTENANCE ───────────────────────────────────────────────────────
        # Finalize any pending exits that have passed the confirmation window
        from app.services.occupancy_service import process_pending_exits
        await process_pending_exits(db)

        # Finalize all database changes
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(f"Dispatch failed, transaction rolled back: {e}", exc_info=True)
        raise

    # Snapshot: Independent from DB transaction
    if event.event_type in ("fielddetection", "linedetection", "regionEntrance", "AccessControllerEvent"):
        try:
            await fetch_snapshot(event.camera_id, event.event_type)
        except Exception as e:
            logger.warning(f"Snapshot fetch failed for {event.camera_id}: {e}")