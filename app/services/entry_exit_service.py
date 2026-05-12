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

from datetime import timedelta
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.services import parking_session_service
from app.services import vehicle_service
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings, facility_now_naive, facility_tz
from app.utils.logger import get_logger
from app.utils import core_backend_client

logger = get_logger(__name__)

# Pending entry cache: ANPR fired but car hasn't crossed CAM-03 yet.
# Key: plate_number. Value: dict with all data needed to write entry_exit_log.
# Entries expire after PENDING_ENTRY_TTL_SECONDS if CAM-03 never fires (false trigger).
_pending_entries: dict = {}
PENDING_ENTRY_TTL_SECONDS = 60

# Pre-confirmation cache: CAM-03 fired BEFORE the ANPR event arrived.
# In practice, CAM-03 hardware line detection fires in milliseconds while
# ANPR plate recognition takes 1–3 s on the camera — so CAM-03 almost always
# wins the race. Storing a timestamp here lets the incoming ANPR event write
# immediately instead of deferring (which would stall forever).
_cam03_pre_confirmations: list = []
CAM03_PRE_CONFIRM_TTL_SECONDS = 10


def _cleanup_pending(db=None):
    now = facility_now_naive()
    expired = [
        p for p, d in _pending_entries.items()
        if (now - d["event_time"]).total_seconds() > PENDING_ENTRY_TTL_SECONDS
    ]
    for p in expired:
        pending = _pending_entries[p]
        if db and pending.get("vehicle_newly_created") and pending.get("vehicle_id"):
            # The vehicle row was inserted by this ANPR event and CAM-03 never
            # confirmed — the car didn't actually enter. Delete the ghost row.
            # is_registered guard: if the plate was registered between ANPR and
            # TTL expiry (60s), skip deletion to avoid destroying real data.
            try:
                from app.models.vehicle import Vehicle as VehicleModel
                db.query(VehicleModel).filter(
                    VehicleModel.id == pending["vehicle_id"],
                    VehicleModel.is_registered == False,
                ).delete(synchronize_session=False)
                logger.info(
                    f"[UC1] Ghost vehicle deleted (CAM-03 never confirmed): "
                    f"plate={p} vehicle_id={pending['vehicle_id']}"
                )
            except Exception as e:
                logger.warning(f"[UC1] Could not delete ghost vehicle plate={p}: {e}")
        else:
            logger.debug(f"[UC1] Pending entry expired (no CAM-03 confirmation): plate={p}")
        del _pending_entries[p]


def _consume_pre_confirmation() -> bool:
    """Consume the oldest valid CAM-03 pre-confirmation. Returns True if one existed."""
    now = facility_now_naive()
    cutoff = now - timedelta(seconds=CAM03_PRE_CONFIRM_TTL_SECONDS)
    _cam03_pre_confirmations[:] = [ts for ts in _cam03_pre_confirmations if ts > cutoff]
    if _cam03_pre_confirmations:
        _cam03_pre_confirmations.pop(0)
        return True
    return False


async def confirm_pending_entry(db: Session):
    """
    Called by occupancy_service when CAM-03 fires in the entry direction.
    Handles both orderings:
      - ANPR first → plate is in _pending_entries → consume and write now.
      - CAM-03 first → store a pre-confirmation token; the arriving ANPR will
        write immediately via _consume_pre_confirmation() instead of deferring.
    """
    _cleanup_pending(db)
    if not _pending_entries:
        # CAM-03 fired before ANPR — store a token so the ANPR, when it arrives,
        # knows it is already confirmed and should write immediately.
        _cam03_pre_confirmations.append(facility_now_naive())
        logger.info("[UC1] CAM-03 fired before ANPR — pre-confirmation stored (ANPR will write on arrival)")
        return

    oldest_plate = min(_pending_entries, key=lambda p: _pending_entries[p]["event_time"])
    pending = _pending_entries.pop(oldest_plate)

    # Re-query vehicle from the current session — the stored vehicle_id is a plain
    # int so it survives across requests; the original ORM object is expired/detached.
    from app.models.vehicle import Vehicle as VehicleModel
    vehicle = (
        db.query(VehicleModel).filter(VehicleModel.id == pending["vehicle_id"]).first()
        if pending["vehicle_id"] else None
    )

    log_entry = EntryExitLog(
        plate_number=pending["plate"],
        vehicle_id=pending["vehicle_id"],
        vehicle_type=pending["vehicle_type"],
        gate="entry",
        camera_id=pending["camera_id"],
        event_time=pending["event_time"],
        snapshot_path=pending["snapshot_path"],
        created_at=facility_now_naive(),
    )
    db.add(log_entry)

    parking_session_service.open_session(
        db,
        plate_number=pending["plate"],
        event_time=pending["event_time"],
        camera_id=pending["camera_id"],
        snapshot_path=pending["snapshot_path"],
        vehicle=vehicle,
    )
    logger.info(f"[UC1] Entry CONFIRMED by CAM-03 for plate={oldest_plate}")


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

    # Camera sends tz-aware timestamps like `2026-05-07T12:14:51+03:00`. The
    # DB convention (since 2026-05-07) is NAIVE FACILITY-LOCAL — the wall
    # clock the operator sees, NOT UTC. Convert to facility tz first, then
    # strip tzinfo, so 12:14:51+03:00 stays 12:14:51 in the column. Earlier
    # code converted to UTC and stored 09:14:51, which made the dashboard
    # display 3h behind. astimezone(facility_tz) is idempotent if the value
    # is already in facility tz.
    event_time = event.trigger_time or facility_now_naive()
    if event_time.tzinfo is not None:
        event_time = event_time.astimezone(facility_tz()).replace(tzinfo=None)

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
    from app.repositories.vehicle_repository import vehicle_repo
    existing_vehicle = vehicle_repo.get_by_plate(db, plate)   # None = brand-new plate
    vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
    vehicle_newly_created = existing_vehicle is None           # we just inserted this row
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"
    owner_name = vehicle.owner_name if vehicle else "Unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type}")

    # ── ENTRY ─────────────────────────────────────────────────────────────
    if gate == "entry":
        if settings.USE_CAM03_ENTRY_CONFIRMATION:
            if _consume_pre_confirmation():
                # CAM-03 already fired before this ANPR arrived — write immediately.
                log_entry = EntryExitLog(
                    plate_number=plate,
                    vehicle_id=vehicle.id if vehicle else None,
                    vehicle_type=vehicle_type,
                    gate="entry",
                    camera_id=event.camera_id,
                    event_time=event_time,
                    snapshot_path=event.snapshot_path,
                    created_at=facility_now_naive(),
                )
                db.add(log_entry)
                parking_session_service.open_session(
                    db,
                    plate_number=plate,
                    event_time=event_time,
                    camera_id=event.camera_id,
                    snapshot_path=event.snapshot_path,
                    vehicle=vehicle,
                )
                logger.info(f"[UC1] Entry CONFIRMED immediately (CAM-03 pre-confirmed): plate={plate}")
            else:
                # No pre-confirmation — defer DB write; CAM-03 will call confirm_pending_entry()
                # Store plain scalars only — the Vehicle ORM object is bound to this
                # request's session, which closes after db.commit(). Storing the object
                # itself causes a "detached instance" error in the next request's session.
                _cleanup_pending(db)
                _pending_entries[plate] = {
                    "plate": plate,
                    "camera_id": event.camera_id,
                    "event_time": event_time,
                    "snapshot_path": event.snapshot_path,
                    "vehicle_id": vehicle.id if vehicle else None,
                    "vehicle_type": vehicle_type,
                    "is_employee": bool(vehicle.is_employee) if vehicle else False,
                    "vehicle_newly_created": vehicle_newly_created,
                }
                logger.info(f"[UC1] ANPR entry pending CAM-03 confirmation: plate={plate}")
        else:
            # Immediate write (USE_CAM03_ENTRY_CONFIRMATION=False)
            log_entry = EntryExitLog(
                plate_number=plate,
                vehicle_id=vehicle.id if vehicle else None,
                vehicle_type=vehicle_type,
                gate="entry",
                camera_id=event.camera_id,
                event_time=event_time,
                snapshot_path=event.snapshot_path,
                created_at=facility_now_naive(),
            )
            db.add(log_entry)
            parking_session_service.open_session(
                db,
                plate_number=plate,
                event_time=event_time,
                camera_id=event.camera_id,
                snapshot_path=event.snapshot_path,
                vehicle=vehicle,
            )
            logger.info(f"[UC1] Entry LOGGED immediately: plate={plate}")

        # UC4: alert/notification fires regardless of two-phase mode
        if vehicle and vehicle.is_registered:
            from app.services.alert_service import broadcast_event
            await broadcast_event(
                is_alert=False,
                severity="info",
                event_type="AccessControllerEvent",
                description=f"Registered vehicle at entry gate: plate {plate}",
                camera_id=event.camera_id,
                zone_id=gate,
                plate_number=plate,
                snapshot_path=event.snapshot_path,
                triggered_at=event_time
            )
        else:
            logger.info(f"[UC4] Triggering alert for unknown/unregistered vehicle: {plate}")
            await create_alert(
                db=db,
                alert_type="unknown_vehicle",
                camera_id=event.camera_id,
                zone_id=gate,
                event_type="AccessControllerEvent",
                description=f"Unregistered vehicle at entry gate: plate {plate}",
                plate_number=plate,
                snapshot_path=event.snapshot_path,
            )
        return

    # ── EXIT: log immediately, no confirmation needed ─────────────────────
    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate=gate,
        camera_id=event.camera_id,
        event_time=event_time,
        snapshot_path=event.snapshot_path,
        created_at=facility_now_naive(),
    )

    # UC2: Calculation of Parking Duration on Exit
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
        t2 = matching_entry.event_time
        if t2.tzinfo is not None:
            t2 = t2.astimezone(facility_tz()).replace(tzinfo=None)

        duration_seconds = int((event_time - t2).total_seconds())
        log_entry.parking_duration = max(0, duration_seconds)

        db.add(log_entry)
        db.flush()

        log_entry.matched_entry_id = matching_entry.id
        matching_entry.matched_entry_id = log_entry.id

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

    # UC4
    if vehicle and vehicle.is_registered:
        from app.services.alert_service import broadcast_event
        await broadcast_event(
            is_alert=False,
            severity="info",
            event_type="AccessControllerEvent",
            description=f"Registered vehicle at exit gate: plate {plate}",
            camera_id=event.camera_id,
            zone_id=gate,
            plate_number=plate,
            snapshot_path=event.snapshot_path,
            triggered_at=event_time
        )
    else:
        logger.info(f"[UC4] Triggering alert for unknown/unregistered vehicle: {plate}")
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type="AccessControllerEvent",
            description=f"Unregistered vehicle at exit gate: plate {plate}",
            plate_number=plate,
            snapshot_path=event.snapshot_path,
        )

    if log_entry not in db.new:
        db.add(log_entry)
