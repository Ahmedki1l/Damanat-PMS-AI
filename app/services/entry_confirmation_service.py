"""Apply validated VA entry decisions within one SQL transaction.

This path preserves VA's plate identity and performs no external network I/O.
Optional decision receipts commit with the entry log and session; legacy
camera/plate/time reconciliation remains available for older decisions.
"""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import threading
from typing import Iterator, Literal, Optional

from sqlalchemy.orm import Session

from app.config import facility_now_naive, facility_tz, settings
from app.models.entry_exit_log import EntryExitLog
from app.models.entry_confirmation_receipt import EntryConfirmationReceipt
from app.models.parking_session import ParkingSession
from app.schemas.entry_confirmation import EntryConfirmationRequest
from app.services import parking_session_service, vehicle_service
from app.services.entry_attempt_snapshot_store import resolve_entry_attempt_snapshot
from app.services.entry_state_lock import (
    acquire_mssql_application_lock as _acquire_mssql_application_lock,
    plate_lock_resource,
)
from app.services.event_parser import normalize_plate
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Serializes confirmations inside one worker. SQL Server's transaction-owned
# application lock below extends the same natural-key guard across workers.
_confirmation_lock = threading.Lock()


@dataclass(frozen=True)
class ConfirmationApplyResult:
    result: Literal["created", "duplicate", "stale_after_exit"]
    plate_number: str
    entry_log_id: int
    session_id: int


class StaleAfterExit(Exception):
    """The entry decision arrived after its vehicle had already exited."""

    def __init__(self, plate_number: str, message: str):
        super().__init__(message)
        self.plate_number = plate_number


class SupersededByNewerEntry(Exception):
    """The confirmed callback is older than already-authoritative stay state."""

    def __init__(self, plate_number: str, message: str):
        super().__init__(message)
        self.plate_number = plate_number


class DecisionReceiptConflict(ValueError):
    """A decision ID was reused with different confirmation content."""


class InvalidEntryConfirmation(ValueError):
    """A syntactically valid callback contains an unusable plate identity."""


def _event_time(body: EntryConfirmationRequest) -> datetime:
    value = body.entry_captured_at
    if value.tzinfo is not None:
        return value.astimezone(facility_tz()).replace(tzinfo=None)
    return value


def _lock_resource(body: EntryConfirmationRequest) -> str:
    # The duplicate lookup intentionally accepts a small timestamp tolerance
    # for SQL Server DATETIME rounding. Serializing per entry camera keeps that
    # entire tolerance window under one lock; hashing the exact timestamp here
    # would let two near-identical callbacks race under different locks.
    raw = body.entry_camera_id
    return "entry-v2:" + hashlib.sha256(raw.encode()).hexdigest()


def _plate_lock_resource(body: EntryConfirmationRequest) -> str:
    return plate_lock_resource(body.canonical_plate or "")


@contextmanager
def confirmation_transaction_guard(
    db: Session,
    body: EntryConfirmationRequest,
) -> Iterator[None]:
    """Hold replay serialization until the router commits or rolls back."""
    with _confirmation_lock:
        # Camera scope protects the timestamp-tolerance idempotency predicate;
        # plate scope protects the one-open-stay invariant across entry cameras.
        resources = {_lock_resource(body), _plate_lock_resource(body)}
        if settings.ENTRY_V2_CONFIRMATION_RECEIPTS_ENABLED:
            resources.add("entry-decision:" + hashlib.sha256(body.decision_id.encode()).hexdigest())
        for resource in sorted(resources):
            _acquire_mssql_application_lock(db, resource)
        yield


def _find_existing_log(
    db: Session,
    body: EntryConfirmationRequest,
    canonical_plate: str,
) -> Optional[EntryExitLog]:
    event_time = _event_time(body)
    # SQL Server DATETIME can round sub-millisecond values. A tight tolerance
    # keeps retries stable after that round-trip without merging real entries.
    tolerance = timedelta(milliseconds=10)
    return (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.gate == "entry",
            EntryExitLog.camera_id == body.entry_camera_id,
            EntryExitLog.plate_number == canonical_plate,
            EntryExitLog.event_time >= event_time - tolerance,
            EntryExitLog.event_time <= event_time + tolerance,
        )
        .order_by(EntryExitLog.id.asc())
        .first()
    )


def _find_matching_session(
    db: Session,
    log_entry: EntryExitLog,
) -> Optional[ParkingSession]:
    tolerance = timedelta(milliseconds=10)
    return (
        db.query(ParkingSession)
        .filter(
            ParkingSession.plate_number == log_entry.plate_number,
            ParkingSession.entry_camera_id == log_entry.camera_id,
            ParkingSession.entry_time >= log_entry.event_time - tolerance,
            ParkingSession.entry_time <= log_entry.event_time + tolerance,
        )
        .order_by(ParkingSession.id.asc())
        .first()
    )


def _find_later_exit(
    db: Session,
    plate_number: str,
    entry_time: datetime,
) -> Optional[EntryExitLog]:
    return (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate_number,
            EntryExitLog.gate == "exit",
            EntryExitLog.event_time >= entry_time,
        )
        .order_by(EntryExitLog.event_time.asc(), EntryExitLog.id.asc())
        .first()
    )


def _reconcile_older_open_sessions(
    db: Session,
    plate_number: str,
    entry_time: datetime,
) -> None:
    """Close stale stays proven obsolete by a validated physical re-entry."""
    open_sessions = parking_session_service.get_open_sessions(db, plate_number)
    blocking = [
        session
        for session in open_sessions
        if session.entry_time >= entry_time
    ]
    if blocking:
        newest = max(blocking, key=lambda session: session.entry_time)
        raise SupersededByNewerEntry(
            plate_number,
            "an equal or newer open parking session already exists for plate "
            f"{plate_number} at {newest.entry_time.isoformat()}; the older "
            "confirmation was not applied",
        )

    for session in open_sessions:
        parking_session_service.reconcile_open_session_for_reentry(
            db,
            session,
            entry_time,
        )
    if open_sessions:
        db.flush()


def _fill_missing_entry_snapshots(
    log_entry: EntryExitLog,
    session: Optional[ParkingSession],
    snapshot_path: Optional[str],
) -> None:
    """Repair an incomplete replay without replacing either accepted image."""
    if not snapshot_path:
        return
    if not log_entry.snapshot_path:
        log_entry.snapshot_path = snapshot_path
    if session is not None and not session.entry_snapshot_path:
        session.entry_snapshot_path = snapshot_path


def apply_confirmed_entry(
    db: Session,
    body: EntryConfirmationRequest,
) -> ConfirmationApplyResult:
    """Persist a successful receipt in the caller's session transaction.

    Lost-response replays return the original IDs even after the visit exits;
    they never reopen a stay. Legacy camera/time reconciliation remains the
    fallback for decisions accepted before receipts were enabled.
    """
    if not settings.ENTRY_V2_CONFIRMATION_RECEIPTS_ENABLED:
        return _apply_confirmed_entry(db, body)
    payload = body.model_dump(mode="json")
    # Normalize the timestamp to the same facility wall clock used by SQL.
    payload["entry_captured_at"] = _event_time(body).isoformat()
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()).hexdigest()
    receipt = db.get(EntryConfirmationReceipt, body.decision_id)
    if receipt is not None:
        if receipt.request_fingerprint != fingerprint:
            raise DecisionReceiptConflict("decision_id already belongs to different confirmation content")
        session = db.get(ParkingSession, receipt.session_id)
        if session is None:
            raise InvalidEntryConfirmation("receipt references a missing parking session")
        # A receipt proves the historical commit, not that the car is still
        # inside. The VA ACK contract suppresses identity publication on stale.
        result = (
            "stale_after_exit"
            if session.exit_time is not None or session.status != "open"
            else "duplicate"
        )
        return ConfirmationApplyResult(
            result=result, plate_number=receipt.plate_number,
            entry_log_id=receipt.entry_log_id, session_id=receipt.session_id,
        )
    applied = _apply_confirmed_entry(db, body)
    db.add(EntryConfirmationReceipt(
        decision_id=body.decision_id, request_fingerprint=fingerprint,
        plate_number=applied.plate_number, entry_log_id=applied.entry_log_id,
        session_id=applied.session_id, created_at=facility_now_naive(),
    ))
    db.flush()
    return applied


def _apply_confirmed_entry(
    db: Session,
    body: EntryConfirmationRequest,
) -> ConfirmationApplyResult:
    """Create/reuse one entry log and one parking session, without I/O awaits."""
    canonical_plate = normalize_plate(body.canonical_plate or "")
    if not canonical_plate:
        raise InvalidEntryConfirmation(
            "canonical_plate is not a valid plate identity"
        )

    snapshot_path = resolve_entry_attempt_snapshot(body.attempt_id)

    existing_log = _find_existing_log(db, body, canonical_plate)
    if existing_log is not None:
        snapshot_path = existing_log.snapshot_path or snapshot_path
        session = _find_matching_session(db, existing_log)
        later_exit = _find_later_exit(
            db,
            existing_log.plate_number,
            existing_log.event_time,
        )
        session_ended = session is not None and (
            session.status != "open" or session.exit_time is not None
        )
        if later_exit is not None or session_ended:
            raise StaleAfterExit(
                existing_log.plate_number,
                "confirmation matches an entry whose stay has already ended; "
                "no open stay was recreated",
            )
        if session is None:
            _reconcile_older_open_sessions(
                db,
                existing_log.plate_number,
                existing_log.event_time,
            )
            vehicle = vehicle_service.ensure_unregistered_vehicle(
                db, existing_log.plate_number
            )
            session = parking_session_service.open_session(
                db,
                plate_number=existing_log.plate_number,
                event_time=existing_log.event_time,
                camera_id=existing_log.camera_id,
                snapshot_path=snapshot_path,
                vehicle=vehicle,
            )
            from app.services.occupancy_service import (
                reconcile_zone_counts_from_open_sessions,
            )

            reconcile_zone_counts_from_open_sessions(
                db,
                camera_id=body.entry_camera_id,
            )
            db.flush()
        _fill_missing_entry_snapshots(existing_log, session, snapshot_path)
        return ConfirmationApplyResult(
            result="duplicate",
            plate_number=existing_log.plate_number,
            entry_log_id=existing_log.id,
            session_id=session.id,
        )

    event_time = _event_time(body)
    later_exit = _find_later_exit(db, canonical_plate, event_time)
    if later_exit is not None:
        raise StaleAfterExit(
            canonical_plate,
            "a later exit is already committed for plate "
            f"{canonical_plate}; no open stay was created",
        )

    _reconcile_older_open_sessions(db, canonical_plate, event_time)

    vehicle = vehicle_service.ensure_unregistered_vehicle(db, canonical_plate)
    log_entry = EntryExitLog(
        plate_number=canonical_plate,
        vehicle_id=vehicle.id,
        vehicle_type=vehicle.vehicle_type,
        gate="entry",
        camera_id=body.entry_camera_id,
        event_time=event_time,
        snapshot_path=snapshot_path,
        plate_confidence=body.plate_confidence,
        created_at=facility_now_naive(),
    )
    db.add(log_entry)
    db.flush()
    session = parking_session_service.open_session(
        db,
        plate_number=canonical_plate,
        event_time=log_entry.event_time,
        camera_id=body.entry_camera_id,
        snapshot_path=snapshot_path,
        vehicle=vehicle,
    )
    from app.services.occupancy_service import (
        reconcile_zone_counts_from_open_sessions,
    )

    reconcile_zone_counts_from_open_sessions(
        db,
        camera_id=body.entry_camera_id,
    )
    db.flush()
    return ConfirmationApplyResult(
        result="created",
        plate_number=canonical_plate,
        entry_log_id=log_entry.id,
        session_id=session.id,
    )
