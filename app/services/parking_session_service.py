"""
Service helpers for the parking_sessions read model.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings, facility_now_naive, facility_tz
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _naive(dt: Optional[datetime]) -> datetime:
    """Normalize a (possibly tz-aware) datetime to NAIVE FACILITY-LOCAL for DB
    columns. Convert tz-aware values to facility tz before stripping tzinfo
    so the stored value matches the operator's wall clock. The DB convention
    shifted 2026-05-07 from naive UTC to naive facility-local — every writer
    must agree, otherwise sessions and entry-exit rows drift apart by the
    facility offset."""
    value = dt or facility_now_naive()
    if value.tzinfo is not None:
        return value.astimezone(facility_tz()).replace(tzinfo=None)
    return value


def _resolve_vehicle(db: Session, session: ParkingSession) -> Optional[Vehicle]:
    """Find the Vehicle row referenced by a parking session.

    Prefers `session.vehicle_id` when set; falls back to plate-matching
    for legacy sessions written before the FK was always populated.
    Returns None if no matching vehicle exists.
    """
    if session.vehicle_id:
        v = db.query(Vehicle).filter(Vehicle.id == session.vehicle_id).first()
        if v:
            return v
    if session.plate_number:
        return db.query(Vehicle).filter(Vehicle.plate_number == session.plate_number).first()
    return None


def get_latest_open_session(db: Session, plate_number: str) -> Optional[ParkingSession]:
    return (
        db.query(ParkingSession)
        .filter(
            ParkingSession.plate_number == plate_number,
            ParkingSession.status == "open",
        )
        .order_by(ParkingSession.entry_time.desc(), ParkingSession.id.desc())
        .first()
    )


def open_session(
    db: Session,
    plate_number: str,
    event_time: datetime,
    camera_id: str,
    snapshot_path: Optional[str],
    vehicle: Optional[Vehicle] = None,
) -> ParkingSession:
    existing = get_latest_open_session(db, plate_number)
    if existing:
        existing.updated_at = facility_now_naive()
        if snapshot_path and not existing.entry_snapshot_path:
            existing.entry_snapshot_path = snapshot_path
        return existing

    now = facility_now_naive()
    session = ParkingSession(
        plate_number=plate_number,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle.vehicle_type if vehicle else "unknown",
        is_employee=bool(vehicle.is_employee) if vehicle else False,
        entry_time=_naive(event_time),
        entry_camera_id=camera_id,
        entry_snapshot_path=snapshot_path,
        status="open",
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    return session


def close_session(
    db: Session,
    plate_number: str,
    event_time: datetime,
    camera_id: str,
    snapshot_path: Optional[str],
) -> Optional[ParkingSession]:
    session = get_latest_open_session(db, plate_number)
    if not session:
        logger.warning("[ParkingSession] No open session found for plate=%s", plate_number)
        return None

    exit_time = _naive(event_time)
    session.exit_time = exit_time
    session.exit_camera_id = camera_id
    session.exit_snapshot_path = snapshot_path
    session.duration_seconds = max(0, int((exit_time - session.entry_time).total_seconds()))
    session.status = "closed"
    session.updated_at = facility_now_naive()
    # If the car exited while still bound to a slot (no VA unbind before the
    # ANPR exit event), close_session is the last writer that knows when the
    # slot was vacated. Backfill slot_left_at so historical queries can rely
    # on `slot_left_at IS NOT NULL` for closed sessions.
    if session.slot_id is not None and session.slot_left_at is None:
        session.slot_left_at = exit_time

    # The car has left the garage entirely — clear all "where is it now"
    # mirrors on the vehicle row. Without this, the vehicles row keeps
    # pointing at the last slot/floor indefinitely.
    vehicle = _resolve_vehicle(db, session)
    if vehicle is not None:
        vehicle.current_slot_id = None
        vehicle.floor = None
        vehicle.floor_id = None

    return session


def bind_slot(
    db: Session,
    plate_number: str,
    slot_id: str,
    slot_number: str,
    zone_id: str,
    zone_name: Optional[str],
    floor: Optional[str],
    camera_id: str,
    parked_at: Optional[datetime],
    snapshot_path: Optional[str],
) -> ParkingSession:
    session = get_latest_open_session(db, plate_number)
    if not session:
        raise LookupError(f"No open parking session found for plate {plate_number}")

    zone_meta = settings.get_zone_metadata(zone_id)
    session.slot_id = slot_id
    session.slot_number = slot_number
    session.zone_id = zone_id
    session.zone_name = zone_name or zone_meta.get("zone_name") or zone_id
    session.floor = floor or zone_meta.get("floor")
    session.parked_at = _naive(parked_at) if parked_at else facility_now_naive()
    session.slot_camera_id = camera_id
    if snapshot_path:
        session.slot_snapshot_path = snapshot_path
    # Clear slot_left_at on (re-)bind so the Gateway can rely on
    # `slot_left_at IS NOT NULL` meaning "currently between slots".
    # Without this, a B1->B2 move leaves the old unbind timestamp in place
    # and downstream consumers think the car has already left B16.
    session.slot_left_at = None
    session.updated_at = facility_now_naive()

    # Mirror the slot binding onto the vehicle row so callers that only
    # have the Vehicle (not the open session) can still answer
    # "where is this vehicle right now?". Write floor too — VA also writes
    # it on every track confirmation, but a fresh bind is the latest
    # signal so it should win.
    vehicle = _resolve_vehicle(db, session)
    if vehicle is not None:
        vehicle.current_slot_id = slot_id
        if session.floor:
            vehicle.floor = session.floor

    db.flush()
    return session


def unbind_slot(
    db: Session,
    plate_number: str,
    camera_id: str,
    left_at: Optional[datetime],
    snapshot_path: Optional[str],
    slot_id: Optional[str] = None,
    slot_number: Optional[str] = None,
) -> ParkingSession:
    session = get_latest_open_session(db, plate_number)
    if not session:
        raise LookupError(f"No open parking session found for plate {plate_number}")
    if slot_number and session.slot_number and session.slot_number != slot_number:
        raise ValueError(
            f"Open parking session for plate {plate_number} is bound to slot {session.slot_number}, not {slot_number}"
        )
    if slot_id and session.slot_id and session.slot_id != slot_id:
        raise ValueError(
            f"Open parking session for plate {plate_number} is bound to slot id {session.slot_id}, not {slot_id}"
        )

    session.slot_left_at = _naive(left_at) if left_at else facility_now_naive()
    session.slot_camera_id = camera_id
    if snapshot_path:
        session.slot_snapshot_path = snapshot_path
    session.updated_at = facility_now_naive()

    # Clear the slot binding on the vehicle, but only if it still points
    # at the slot we're unbinding — otherwise we'd clobber a concurrent
    # bind that already moved the vehicle to another slot.
    vehicle = _resolve_vehicle(db, session)
    if vehicle is not None and vehicle.current_slot_id == session.slot_id:
        vehicle.current_slot_id = None

    db.flush()
    return session

