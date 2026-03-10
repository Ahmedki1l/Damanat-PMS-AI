"""
Phase 2: UC1 (Entry/Exit Counting), UC2 (Parking Time), and UC4 (Vehicle ID).
Handles AccessControllerEvent from ANPR cameras.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.services import vehicle_service
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

async def handle_anpr_event(event: ParsedCameraEvent, db: Session):
    """
    Process ANPR events to log vehicle movement, identify owners, 
    and calculate parking duration for exits.
    """
    plate = event.plate_number

    camera_config = settings.CAMERAS.get(event.camera_id, {})
    gate = camera_config.get("gate", event.gate)

    if not plate:
        logger.warning(f"[Phase2] ANPR event with no plate from {event.camera_id} - skipped")
        return

    # UC4: Resolve vehicle identity via vehicle_service
    vehicle = vehicle_service.lookup_vehicle(db, plate)
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"
    owner_name = vehicle.owner_name if vehicle else "Unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type}")

    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate=gate,
        camera_id=event.camera_id,
        event_time=event.trigger_time or datetime.utcnow(),
        created_at=datetime.utcnow(),
    )

    # UC2: Calculation of Parking Duration on Exit
    if gate == "exit":
        matching_entry = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == plate,
                EntryExitLog.gate == "entry",
                EntryExitLog.matched_entry_id.is_(None)
            )
            .order_by(EntryExitLog.event_time.desc())
            .first()
        )

        if matching_entry:
            # Normalize both to naive UTC to avoid "can't subtract offset-naive and offset-aware datetimes"
            t1 = log_entry.event_time.replace(tzinfo=None) if log_entry.event_time.tzinfo else log_entry.event_time
            t2 = matching_entry.event_time.replace(tzinfo=None) if matching_entry.event_time.tzinfo else matching_entry.event_time
            
            duration_seconds = int((t1 - t2).total_seconds())
            log_entry.parking_duration = max(0, duration_seconds)

            db.add(log_entry)
            db.flush()

            log_entry.matched_entry_id = matching_entry.id
            matching_entry.matched_entry_id = log_entry.id

            # Calculate minutes and seconds for a clearer log
            mins, secs = divmod(duration_seconds, 60)
            logger.info(f"[UC2] ✅ MATCH FOUND! Vehicle {plate} parked for {mins}m {secs}s")
        else:
            logger.warning(f"[UC2] ❌ No matching entry found for vehicle {plate}")

    # UC4: Alert for unregistered vehicles
    if not vehicle:
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type="AccessControllerEvent",
            description=f"Unregistered vehicle at {gate} gate: plate {plate}",
        )

    if log_entry not in db.new:
        db.add(log_entry)