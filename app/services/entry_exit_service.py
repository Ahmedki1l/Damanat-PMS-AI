"""
Phase 2: UC1 (Entry/Exit Counting), UC2 (Parking Time), and UC4 (Vehicle ID).
Handles AccessControllerEvent / ANPR from ANPR cameras.

Note: `vehicleMatchResult` is intentionally NOT handled here. The dispatcher
suppresses it (see event_dispatcher.py) because Hikvision fires it without
an inline multipart image, which forces an ISAPI snapshot pull that some
camera ACL configs reject. The follow-on `ANPR` event (~1-5s later) carries
the multipart JPEG and drives row creation here. This makes entry-camera
behaviour identical to exit-camera behaviour: both rely on the inline image,
neither hits the ISAPI snapshot endpoint.
"""

from datetime import datetime, UTC, timedelta
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.services import parking_session_service
from app.services import vehicle_service
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger
from app.utils import core_backend_client

logger = get_logger(__name__)

async def handle_anpr_event(event: ParsedCameraEvent, db: Session):
    """
    Process ANPR events to log vehicle movement, identify owners,
    and calculate parking duration for exits.
    """
    plate = event.plate_number

    camera_config = settings.CAMERAS.get(event.camera_id, {})
    gate = camera_config.get("gate", event.gate)

    if not gate:
        logger.warning(f"[Phase2] Dropped ANPR event from {event.camera_id} - no gate assigned (IP or Serial mapping missing)")
        return

    if not plate:
        logger.debug(f"[Phase2] ANPR event with no plate from {event.camera_id} - skipped")
        return

    # Convert to UTC then strip tzinfo: camera sends tz-aware timestamps (e.g.
    # `2026-03-11T08:58+03:00`), but DB columns are naive UTC. The earlier code
    # called `.replace(tzinfo=None)` directly, which silently dropped the offset
    # and stored facility-local datetimes pretending to be UTC. Fix: convert
    # first, then strip. Historical rows pre-fix are 3h ahead of true UTC
    # (assuming Saudi +03:00 cameras) — see sql/migrate_facility_local_to_utc.sql
    # for the one-time backfill that aligns the old data.
    event_time = event.trigger_time or datetime.now(UTC)
    # if event_time.tzinfo is not None:
    #     event_time = event_time.astimezone(UTC).replace(tzinfo=None)

    # Deduplication: check for recent events
    logger.debug(f"[UC1] Checking dedup for plate {plate}...")
    dedup_window = event_time - timedelta(seconds=30)
    recent = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == gate,
            EntryExitLog.event_time >= dedup_window,
        )
        .first()
    )
    if recent:
        logger.debug(f"[UC1] Duplicate suppressed for plate={plate} gate={gate}")
        return

    # Forward plate + snapshot to PMS tracking API (fire-and-forget)
    if gate in ["entry", "exit"]:
        try:
            await core_backend_client.notify_pms_anpr(
                plate, gate, image_path=event.local_snapshot_path or event.snapshot_path,
            )
        except Exception as e:
            logger.warning(f"[UC1] PMS API forwarding failed for plate={plate}: {e}")

    # Anti-bounce: if this is an entry event but the plate just exited within
    # the last `entry_antibounce_seconds`, the entry camera is likely capturing
    # the car driving away from the exit gate — suppress the false re-entry.
    #
    # Window is env-tunable so deployments with cycling traffic (taxis,
    # delivery vans) don't lose legitimate re-entries. Default 30s — empirical
    # minimum gap between physically passing the exit camera and physically
    # passing the entry camera. Set 0 to disable entirely.
    #
    # The suppression log is INFO (not DEBUG) so ops can see it in default
    # log levels and distinguish anti-bounce from any other silent failure.
    antibounce_s = settings.ENTRY_ANTIBOUNCE_SECONDS
    if gate == "entry" and antibounce_s > 0:
        recent_exit_window = event_time - timedelta(seconds=antibounce_s)
        recent_exit = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == plate,
                EntryExitLog.gate == "exit",
                EntryExitLog.event_time >= recent_exit_window,
            )
            .order_by(EntryExitLog.event_time.desc())
            .first()
        )
        if recent_exit:
            gap_s = (event_time - recent_exit.event_time).total_seconds()
            logger.info(
                "[UC1] Anti-bounce: suppressed entry for plate=%s "
                "(last exit %.1fs ago, window=%ds)",
                plate, gap_s, antibounce_s,
            )
            return

    # UC4: Resolve vehicle identity via vehicle_service
    logger.debug(f"[UC4] Looking up vehicle for plate {plate}...")
    # FIX: Always ensure a vehicle record exists (registered or placeholder)
    # so that log entries have a valid vehicle_id, even on exit.
    vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"
    owner_name = vehicle.owner_name if vehicle else "Unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type}")

    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate=gate,
        camera_id=event.camera_id,
        event_time=event_time,
        snapshot_path=event.snapshot_path,
        created_at=datetime.now(UTC),
    )

    if gate == "entry":
        parking_session_service.open_session(
            db,
            plate_number=plate,
            event_time=event_time,
            camera_id=event.camera_id,
            snapshot_path=event.snapshot_path,
            vehicle=vehicle,
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
            # event_time is already naive (normalized above); matching_entry may
            # still be tz-aware if stored before the fix, so strip it defensively.
            t2 = matching_entry.event_time
            if t2.tzinfo is not None:
                # Convert to UTC before stripping — see comment at line 41.
                t2 = t2.astimezone(UTC).replace(tzinfo=None)

            duration_seconds = int((event_time - t2).total_seconds())
            log_entry.parking_duration = max(0, duration_seconds)

            db.add(log_entry)
            db.flush()

            log_entry.matched_entry_id = matching_entry.id
            matching_entry.matched_entry_id = log_entry.id

            # Calculate minutes and seconds for a clearer log
            mins, secs = divmod(duration_seconds, 60)
            logger.info(f"[UC2] MATCH FOUND! Vehicle {plate} parked for {mins}m {secs}s")
        else:
            logger.warning(f"[UC2] No matching entry found for vehicle {plate}")

        parking_session_service.close_session(
            db,
            plate_number=plate,
            event_time=event_time,
            camera_id=event.camera_id,
            snapshot_path=event.snapshot_path,
        )

    # UC4: Clear logic for single notification/alert
    if vehicle and vehicle.is_registered:
        # Registered vehicle — send single 'info' notification via unified broadcast
        from app.services.alert_service import broadcast_event
        await broadcast_event(
            is_alert=False,
            severity="info",
            event_type="AccessControllerEvent",
            description=f"Registered vehicle at {gate} gate: plate {plate}",
            camera_id=event.camera_id,
            zone_id=gate,
            plate_number=plate,
            snapshot_path=event.snapshot_path,
            triggered_at=event_time
        )
    else:
        # Unregistered vehicle — send single 'critical' alert
        logger.info(f"[UC4] Triggering alert for unknown/unregistered vehicle: {plate}")
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type="AccessControllerEvent",
            description=f"Unregistered vehicle at {gate} gate: plate {plate}",
            plate_number=plate,
            snapshot_path=event.snapshot_path,
        )

    if log_entry not in db.new:
        db.add(log_entry)
