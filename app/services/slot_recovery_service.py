"""Open a parking session for a car VA found parked with no record of it entering.

WHY THIS EXISTS
---------------
A car can be sitting in a slot with no `parking_session` — its entry was missed
entirely (no ANPR read, no HikCentral record, no ramp crossing). It then cannot be
named, cannot be counted, and on exit produces `[UC2] No matching entry found`.
Measured 2026-07-30: BHD-9990 parked in B13, OCR reading `BHD` on 6 of 6 frames,
and the plate appearing NOWHERE in the running system.

VA recovers the identity from two independent witnesses — its persisted appearance
gallery and an OCR read off the car — and posts it here. This module is the only
place that turns that claim into a session.

THE RACE GUARD IS THE POINT
---------------------------
VA's evidence describes a moment. The request may be queued, retried, or delayed
in transit, and parking state moves underneath it: the car leaves, another car
takes the slot, or the slot's plate is re-derived. Acting on stale evidence would
open a session for a car that is no longer there — manufacturing exactly the
phantom this whole effort exists to eliminate.

So the claim is re-validated against `parking_slots` (VA's own live state, in this
same database) at the instant of writing, INSIDE the caller's transaction:

  * the slot must still be OCCUPIED
  * the slot's current_plate must still be the plate being recovered

Either failing rejects the request. A rejection is a normal outcome, not an error —
it means the world moved on, and the correct action is to do nothing.
"""

from datetime import datetime
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import facility_now_naive
from app.models.entry_exit_log import EntryExitLog
from app.models.parking_session import ParkingSession
from app.services import parking_session_service, vehicle_service
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Stamped on the gate log so a recovered entry is never mistaken for a car that
# actually came through the barrier.
RECOVERY_CAMERA_ID = "VA-SLOT-RECOVERY"


class RecoveryRejected(Exception):
    """The world changed between VA observing and PMS-AI writing."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _plate_key(plate: Optional[str]) -> str:
    """Order-independent plate identity.

    The same car is spelled letters-first in the DB (`BHD-9990`) and digits-first
    by the slot OCR (`9990BHD`). Comparing raw strings would reject a correct
    match; comparing (letters, digits) accepts both spellings without accepting a
    different car — `BHD-9990` and `BHD-9909` still differ.
    """
    raw = "".join(c for c in (plate or "").upper() if c.isalnum())
    return ("".join(c for c in raw if c.isalpha())
            + "".join(c for c in raw if c.isdigit()))


def _live_slot_state(db: Session, slot_id: str) -> Tuple[bool, Optional[str]]:
    """(occupied, current_plate) straight from VA's `parking_slots` row.

    Read rather than trusted from the request: this is the whole guard. Uses raw
    SQL because `parking_slots` is VA-owned — PMS-AI has no model for it and should
    not grow one just to read two columns.
    """
    row = db.execute(
        text("SELECT is_available, current_plate FROM parking_slots "
             "WHERE slot_id = :sid"),
        {"sid": slot_id},
    ).first()
    if row is None:
        raise RecoveryRejected(f"slot {slot_id!r} does not exist")
    is_available, current_plate = row[0], row[1]
    return (not bool(is_available), (current_plate or "").strip())


def recover_slot_session(
    db: Session,
    *,
    plate_number: str,
    slot_id: str,
    camera_id: str,
    reid_score: float,
    reid_margin: float,
    reid_same_view: bool,
    ocr_text: str,
    observed_at: Optional[datetime] = None,
) -> Tuple[ParkingSession, bool]:
    """Re-validate, then open a session. Returns (session, created).

    Raises `RecoveryRejected` when the live slot state no longer matches the claim.
    The caller owns the transaction and must commit.
    """
    occupied, live_plate = _live_slot_state(db, slot_id)

    # ---- the race guard ------------------------------------------------------
    if not occupied:
        raise RecoveryRejected(
            f"slot {slot_id!r} is now VACANT — the car left before this landed"
        )
    if not live_plate:
        raise RecoveryRejected(
            f"slot {slot_id!r} no longer names a car — its plate was cleared "
            "after VA observed it"
        )
    if _plate_key(live_plate) != _plate_key(plate_number):
        raise RecoveryRejected(
            f"slot {slot_id!r} now holds {live_plate!r}, not {plate_number!r} — "
            "another car took the slot"
        )
    # -------------------------------------------------------------------------

    # Idempotent by plate: a car already believed inside needs no recovery, and a
    # retried or duplicated request must not open a second session.
    existing = parking_session_service.get_latest_open_session(db, plate_number)
    if existing is not None:
        logger.info(
            "[recovery] %s already has open session id=%s — nothing to recover",
            plate_number, existing.id,
        )
        return existing, False

    when = observed_at or facility_now_naive()
    vehicle = vehicle_service.lookup_vehicle(db, plate_number)

    session = parking_session_service.open_session(
        db,
        plate_number=plate_number,
        event_time=when,
        camera_id=RECOVERY_CAMERA_ID,
        snapshot_path=None,
        vehicle=vehicle,
    )
    # The car is known to be in this slot right now — that is the evidence that
    # justified the session, so record it rather than waiting for VA to bind it
    # again on a later frame.
    session.slot_id = slot_id
    session.slot_camera_id = camera_id
    session.parked_at = when

    # A gate-log row so the stay appears in Entry/Exit like any other, and so the
    # HikCentral reconciler sees this pass as already noticed rather than opening
    # a second session for the same car.
    db.add(EntryExitLog(
        plate_number=plate_number,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle.vehicle_type if vehicle else "unknown",
        gate="entry",
        camera_id=RECOVERY_CAMERA_ID,
        event_time=when,
        created_at=facility_now_naive(),
    ))

    logger.warning(
        "[recovery] OPENED session for %s in slot=%s (cam=%s) — no entry was ever "
        "recorded for this car. Evidence: ReID %.3f margin %.3f (%s) + OCR read %r",
        plate_number, slot_id, camera_id, reid_score, reid_margin,
        "same-view parked pose" if reid_same_view else "cross-view gate photo",
        ocr_text,
    )
    return session, True
