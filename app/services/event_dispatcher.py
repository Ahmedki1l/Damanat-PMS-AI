# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.violation_service import handle_violation_event
from app.services.intrusion_service import handle_intrusion_event
from app.utils.logger import get_logger
from sqlalchemy.orm import Session

logger = get_logger(__name__)


async def dispatch_event(event: ParsedCameraEvent, db: Session):
    is_vehicle = event.detection_target in ("vehicle", None)

    # ── PHASE 1 ───────────────────────────────────────────────────────────
    # UC3: Occupancy — region entrance/exit (CAM-03 only)
    if event.event_type in ("regionEntrance", "regionExiting"):
        await handle_occupancy_event(event, db)

    # UC5: Violation alerts
    if event.event_type in ("fielddetection", "linedetection", "regionEntrance") and is_vehicle:
        await handle_violation_event(event, db)

    # UC6: Intrusion detection
    if event.event_type in ("fielddetection", "regionEntrance") and is_vehicle:
        await handle_intrusion_event(event, db)

    # ── PHASE 2 ───────────────────────────────────────────────────────────
    # UC1 + UC2 + UC4: ANPR gate events
    if event.event_type == "AccessControllerEvent" and event.plate_number:
        try:
            from app.services.entry_exit_service import handle_anpr_event
            await handle_anpr_event(event, db)
        except ImportError:
            logger.warning("entry_exit_service not yet implemented (Phase 2 pending)")
