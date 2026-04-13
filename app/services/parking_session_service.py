"""
Service helpers for the parking_sessions read model.
"""

from datetime import datetime, UTC
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _naive(dt: Optional[datetime]) -> datetime:
    value = dt or datetime.now(UTC)
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


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
        existing.updated_at = datetime.now(UTC)
        if snapshot_path and not existing.entry_snapshot_path:
            existing.entry_snapshot_path = snapshot_path
        return existing

    now = datetime.now(UTC)
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
    session.updated_at = datetime.now(UTC)
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
    session.parked_at = _naive(parked_at) if parked_at else datetime.now(UTC)
    session.slot_camera_id = camera_id
    if snapshot_path:
        session.slot_snapshot_path = snapshot_path
    session.updated_at = datetime.now(UTC)
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

    session.slot_left_at = _naive(left_at) if left_at else datetime.now(UTC)
    session.slot_camera_id = camera_id
    if snapshot_path:
        session.slot_snapshot_path = snapshot_path
    session.updated_at = datetime.now(UTC)
    db.flush()
    return session

