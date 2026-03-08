# app/services/entry_exit_service.py
"""
UC1: Entry/Exit Counting + UC2: Parking Duration.
Handles AccessControllerEvent from ANPR cameras (CAM-ENTRY / CAM-EXIT).
Uses vehicle_service (repository pattern) for vehicle lookup — never queries DB directly.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.services.event_parser import ParsedCameraEvent
from app.services import vehicle_service
from app.services.alert_service import create_alert
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def handle_anpr_event(event: ParsedCameraEvent, db: Session):
    """Process an ANPR gate event: log entry/exit, match pairs, alert on unknowns."""
    plate = event.plate_number
    gate = event.gate  # "entry" or "exit"

    if not plate:
        logger.warning(f"ANPR event with no plate from {event.camera_id} — skipped")
        return

    # UC4: Resolve vehicle identity via repository pattern
    vehicle = vehicle_service.lookup_vehicle(db, plate)
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type}")

    # Create entry/exit log record
    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate=gate,
        camera_id=event.camera_id,
        event_time=event.trigger_time,
        created_at=datetime.utcnow(),
    )
    db.add(log_entry)

    # UC2: If EXIT, find the most recent unmatched ENTRY for same plate
    if gate == "exit":
        matching_entry = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == plate,
                EntryExitLog.gate == "entry",
                EntryExitLog.matched_entry_id.is_(None),
            )
            .order_by(EntryExitLog.event_time.desc())
            .first()
        )
        if matching_entry:
            duration_seconds = int((event.trigger_time - matching_entry.event_time).total_seconds())
            log_entry.matched_entry_id = matching_entry.id
            log_entry.parking_duration = max(0, duration_seconds)
            matching_entry.matched_entry_id = log_entry.id  # cross-reference
            logger.info(f"[UC2] Plate={plate} parked {duration_seconds // 60}m {duration_seconds % 60}s")
        else:
            logger.warning(f"[UC2] No matching entry for plate {plate} at exit")

    # UC4: Alert if vehicle is not registered
    if not vehicle:
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type=event.event_type,
            description=f"Unregistered vehicle at {gate} gate: plate {plate}",
        )
