# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.violation_service import handle_violation_event, RESTRICTED_ZONES, ALWAYS_VIOLATION_EVENTS
from app.services.intrusion_service import handle_intrusion_event, MONITORED_INTRUSION_ZONES
from app.services.entry_exit_service import handle_anpr_event
from app.services.snapshot_service import fetch_snapshot
from app.utils.logger import get_logger
from sqlalchemy.orm import Session

logger = get_logger(__name__)


async def dispatch_event(event: ParsedCameraEvent, db: Session):
    """
    Route event to handlers and commit as a single transaction.
    On any handler failure, rolls back all changes from this dispatch cycle.
    """
    try:
        is_vehicle = event.detection_target in ("vehicle", None)
        is_human = event.detection_target in ("human", None)
        zone_id = event.region_id or ""

        # ── PHASE 1 ───────────────────────────────────────────────────────────
        # UC3: Occupancy — region entrance/exit
        if event.event_type in ("regionEntrance", "regionExiting"):
            await handle_occupancy_event(event, db)

        # UC5 vs UC6: Route fielddetection/regionEntrance/VMD to the correct
        # handler based on zone membership. A zone is either a violation zone
        # OR an intrusion zone, not both.
        if event.event_type in ("fielddetection", "regionEntrance", "VMD") and is_vehicle:
            if zone_id in MONITORED_INTRUSION_ZONES:
                await handle_intrusion_event(event, db)
            elif zone_id in RESTRICTED_ZONES:
                await handle_violation_event(event, db)
            elif event.event_type == "VMD" and not event.region_id:
                # VMD with no zone is generic motion — log and skip
                logger.info(f"VMD motion event from {event.camera_id}, no zone — skipping handlers")
            elif not event.region_id:
                # Non-VMD with no zone info — fire both (services will self-filter)
                await handle_violation_event(event, db)
                await handle_intrusion_event(event, db)

        # linedetection → always violation (vehicles OR humans crossing lines)
        if event.event_type == "linedetection" and (is_vehicle or is_human):
            await handle_violation_event(event, db)

        # ── PHASE 2 ───────────────────────────────────────────────────────────
        # UC1 + UC2 + UC4: ANPR gate events (JSON=AccessControllerEvent, XML=ANPR/vehicleMatchResult)
        if event.event_type in ("AccessControllerEvent", "ANPR", "vehicleMatchResult") and event.plate_number:
            await handle_anpr_event(event, db)

        # Commit all handler changes as a single transaction
        db.commit()

    except Exception as e:
        db.rollback()
        logger.error(f"Dispatch failed, transaction rolled back: {e}", exc_info=True)
        raise

    # Snapshot fetch is fire-and-forget (no DB changes), runs outside transaction
    if event.event_type in ("fielddetection", "linedetection", "regionEntrance", "VMD"):
        try:
            await fetch_snapshot(event.camera_id, event.event_type)
        except Exception as e:
            logger.warning(f"Snapshot fetch failed for {event.camera_id}: {e}")
