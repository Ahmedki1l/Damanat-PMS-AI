"""Rewrite a stay that was opened under a misread plate.

Only the exit can correct an entry. Measured over ai-logs.txt (8/10-8/16): all
144 entry bursts had `reads=1` and an empty `discarded=[...]`, and HikCentral is
fed by the SAME entry LPR, so there are zero `plate_corrected` lines anywhere on
the entry side. Whatever the entry pipeline becomes, nothing in it can catch a
wrong entry plate. The exit read is the first independent look at the car.

So when `exit_match_service` proves an exit belongs to a stay standing under a
different string, that string is a misread and this applies the correction:

    1. a ledger row in `hik_validations` holding BOTH plates
    2. the stay, and the entry log row paired with it
    3. the placeholder `vehicles` row
    4. VA — gallery folder, live session, `parking_slots.current_plate`

**No new tables, no new columns.** The audit trail moves into `hik_validations`,
which already carries `reported_plate` next to `canonical_plate` for exactly this
purpose on the entry side.

The misread is never destroyed. `close_matched_session` used to keep it by
leaving it ON the session — its docstring called it "the only evidence that can
later prove the match right or wrong" — which meant the dashboard showed a car
that was not there. Preserving evidence and displaying it are different jobs:
the ledger row does the first, and the session is now free to do the second.

Historical `slot_status` rows are NEVER rewritten. A correction is an
append-only event plus a current-state update; rewriting history would destroy
the record of what VA actually observed at the time.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.config import facility_now_naive
from app.models.entry_exit_log import EntryExitLog
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import vehicle_service
from app.services.hikcentral.models import PLATE_SOURCE_HIK_CORRECTED
from app.utils.logger import get_logger

logger = get_logger(__name__)

# `hik_validations.guid` is UNIQUE and is normally HikCentral's identity for one
# vehicle pass. A correction proved by the exit read alone has no such identity,
# so it gets a synthetic one under a namespace prefix HikCentral can never emit
# (its GUIDs are bare 32-char hex). Anything that reads the ledger can therefore
# tell "the platform said so" from "our own matcher said so".
LOCAL_GUID_PREFIX = "local:correction:"

# `direction` on the ledger row. The correction is decided by an exit, and the
# row must not be mistaken for an entry pass the reconciler could act on.
CORRECTION_DIRECTION = "exit"


@dataclass(frozen=True)
class Correction:
    """What a correction changed. Empty fields mean "nothing to change here"."""

    misread: str
    plate: str
    session_id: Optional[int] = None
    entry_log_id: Optional[int] = None
    exit_log_id: Optional[int] = None
    vehicle_merged: bool = False
    ledger_guid: Optional[str] = None

    @property
    def applied(self) -> bool:
        return self.misread != self.plate


def local_guid(session_id: Optional[int], when: Optional[datetime] = None) -> str:
    """A ledger identity for a correction HikCentral had no part in."""
    stamp = int((when or facility_now_naive()).timestamp())
    return f"{LOCAL_GUID_PREFIX}{session_id or 0}:{stamp}"


def apply_correction(
    db: Session,
    session: ParkingSession,
    correct_plate: str,
    evidence: str,
    *,
    exit_log: Optional[EntryExitLog] = None,
    hik_guid: Optional[str] = None,
) -> Optional[Correction]:
    """Move a stay and its paper trail onto the plate the car actually carries.

    Adds to the caller's transaction and does NOT commit — a half-applied
    correction (ledger written, session not) is worse than none, so the whole
    thing lives or dies with the caller's commit.

    `evidence` is the matcher's reason, stored verbatim as `match_reason`, so the
    ledger says WHY this car was renamed and not merely that it was.

    Returns None when there is nothing to do. The VA notification is deliberately
    NOT sent here — see `notify_va`, which the caller fires after committing.
    """
    misread = session.plate_number
    if not correct_plate or misread == correct_plate:
        return None

    guid = hik_guid or local_guid(session.id)
    if _guid_taken(db, guid):
        # A replay. The correction already landed under this identity and the
        # ledger's uniqueness is what makes that detectable.
        logger.info(
            "[Correction] %s -> %s already recorded under guid=%s — skipping",
            misread, correct_plate, guid,
        )
        return None

    db.add(
        HikValidation(
            session_id=session.id,
            entry_exit_log_id=exit_log.id if exit_log is not None else None,
            direction=CORRECTION_DIRECTION,
            guid=guid,
            plate_license=None,
            canonical_plate=correct_plate,
            # The whole point of the row: the misread survives the rewrite.
            reported_plate=misread,
            plate_source=PLATE_SOURCE_HIK_CORRECTED,
            pass_time=session.exit_time or facility_now_naive(),
            matched=True,
            match_reason=evidence,
            created_at=facility_now_naive(),
        )
    )

    vehicle, merged = _resolve_vehicle(db, misread, correct_plate)
    entry_log = _paired_entry_log(db, session, misread)

    session.plate_number = correct_plate
    if vehicle is not None:
        session.vehicle_id = vehicle.id
        session.vehicle_type = vehicle.vehicle_type
    if entry_log is not None:
        entry_log.plate_number = correct_plate
        if vehicle is not None:
            entry_log.vehicle_id = vehicle.id
            entry_log.vehicle_type = vehicle.vehicle_type
    if exit_log is not None:
        exit_log.plate_number = correct_plate
        if vehicle is not None:
            exit_log.vehicle_id = vehicle.id
            exit_log.vehicle_type = vehicle.vehicle_type

    logger.warning(
        "[Correction] session id=%s %s -> %s (%s) — misread preserved in "
        "hik_validations guid=%s",
        session.id, misread, correct_plate, evidence, guid,
    )
    return Correction(
        misread=misread,
        plate=correct_plate,
        session_id=session.id,
        entry_log_id=entry_log.id if entry_log is not None else None,
        exit_log_id=exit_log.id if exit_log is not None else None,
        vehicle_merged=merged,
        ledger_guid=guid,
    )


async def notify_va(correction: Optional[Correction]) -> bool:
    """Tell VA the car moved, AFTER the caller committed.

    Separate from `apply_correction` for two reasons: a network call has no place
    inside a transaction holding write locks, and VA must be told about a
    correction that is already durable — telling it first and then rolling back
    would leave VA holding a plate PMS-AI never adopted.

    Never raises, and the guard is here rather than only in the client: the
    client catches httpx's own transport errors, but a refused connection, a TLS
    failure outside that hierarchy or a bug in the client would otherwise escape.
    The reconcile sweep AWAITS this directly after its commit, so an escape there
    would abandon the remaining records of a catch-up chunk — one VA hiccup
    costing a whole sweep, long after the correction it was reporting is durable.
    """
    if correction is None or not correction.applied:
        return False
    from app.utils import va_reid_client

    try:
        return await va_reid_client.rename(correction.misread, correction.plate)
    except Exception as exc:
        logger.warning(
            "[Correction] VA was not told about %s -> %s: %r. The correction "
            "stands; the exit sweep re-applies it.",
            correction.misread, correction.plate, exc,
        )
        return False


def _guid_taken(db: Session, guid: str) -> bool:
    return (
        db.query(HikValidation.id).filter(HikValidation.guid == guid).first()
        is not None
    )


def _resolve_vehicle(
    db: Session, misread: str, correct_plate: str
) -> tuple[Optional[Vehicle], bool]:
    """The `vehicles` row the corrected plate should point at.

    `plate_number` is UNIQUE, so the placeholder minted under the misread cannot
    simply be renamed when a row for the correct plate already exists — and it
    often does, because the car has been here before under its real plate.

    Two outcomes, and the distinction matters: with no existing row the
    placeholder is RENAMED in place, so anything already pointing at it follows
    the rename. With one, the correct row wins and the placeholder is left alone
    rather than deleted — it may still be referenced by rows outside this stay,
    and a dangling FK is a worse outcome than an orphaned placeholder.

    A REGISTERED vehicle is never renamed: a human vouched for that plate, which
    outranks a matcher.
    """
    existing = (
        db.query(Vehicle).filter(Vehicle.plate_number == correct_plate).first()
    )
    if existing is not None:
        return existing, True

    placeholder = (
        db.query(Vehicle).filter(Vehicle.plate_number == misread).first()
    )
    if placeholder is None:
        return vehicle_service.ensure_unregistered_vehicle(db, correct_plate), False
    if getattr(placeholder, "is_registered", False):
        logger.warning(
            "[Correction] %s is a REGISTERED vehicle — not renaming it to %s; "
            "creating the corrected row instead",
            misread, correct_plate,
        )
        return vehicle_service.ensure_unregistered_vehicle(db, correct_plate), False

    placeholder.plate_number = correct_plate
    db.flush()
    return placeholder, False


def _paired_entry_log(
    db: Session, session: ParkingSession, misread: str
) -> Optional[EntryExitLog]:
    """The entry audit row this stay was opened by.

    Matched on the misread plate and the stay's entry time, not on the corrected
    plate — the row still carries the wrong string, which is the reason it is
    being looked up.
    """
    return (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == misread,
            EntryExitLog.gate == "entry",
            EntryExitLog.event_time <= (session.exit_time or facility_now_naive()),
        )
        .order_by(EntryExitLog.event_time.desc())
        .first()
    )
