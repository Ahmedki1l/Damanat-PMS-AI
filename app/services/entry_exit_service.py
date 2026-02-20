# app/services/entry_exit_service.py
"""
Phase 2: UC1 (Entry/Exit Counting) + UC2 (Parking Time) + UC4 (Vehicle ID)
Camera: CAM-ENTRY (192.168.1.104) + CAM-EXIT (192.168.1.105)
Event: AccessControllerEvent (JSON)
Key field: event.plate_number (= cardNo from camera)

How it works:
  - CAM-ENTRY fires AccessControllerEvent → handle_anpr_event logs ENTRY + resolves vehicle
  - CAM-EXIT fires AccessControllerEvent  → handle_anpr_event logs EXIT + calculates duration
  - Both are stored in entry_exit_log table
  - Daily counts + avg duration derived via SQL queries in parking_stats router
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.models.vehicle import Vehicle
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def handle_anpr_event(event: ParsedCameraEvent, db: Session):
    plate = event.plate_number
    gate = event.gate  # "entry" or "exit"

    if not plate:
        logger.warning(f"[Phase2] ANPR event with no plate from {event.camera_id} — skipped")
        return

    # UC4: Resolve vehicle identity from DB
    vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"
    person_name = vehicle.owner_name if vehicle else event.person_name or "Unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type} | Name={person_name}")

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

    # UC2: If this is an EXIT, find the matching ENTRY and calculate duration
    if gate == "exit":
        matching_entry = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == plate,
                EntryExitLog.gate == "entry",
                EntryExitLog.matched_entry_id == None,  # not yet matched
            )
            .order_by(EntryExitLog.event_time.desc())
            .first()
        )
        if matching_entry:
            duration_seconds = int((event.trigger_time - matching_entry.event_time).total_seconds())
            log_entry.matched_entry_id = matching_entry.id
            log_entry.parking_duration = duration_seconds
            matching_entry.matched_entry_id = log_entry.id  # cross-reference
            logger.info(f"[UC2] Plate={plate} parked for {duration_seconds // 60} min")
        else:
            logger.warning(f"[UC2] No matching entry found for plate {plate} at exit")

    # UC4: Alert if unknown vehicle
    if not vehicle:
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type=event.event_type,
            description=f"Unregistered vehicle at {gate} gate: plate {plate}",
        )

    db.add(log_entry)
    db.commit()
