"""Recovering a session for a car parked with no record of it entering.

The write itself is small. What these tests mostly pin is the RACE GUARD, because
that is the part with teeth: VA's evidence describes a moment, the request can be
queued or retried, and parking state moves underneath it. Acting on stale evidence
would open a session for a car that has already left — manufacturing exactly the
phantom overstay this whole effort exists to remove.

So the refusals are tested harder than the successes.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.entry_exit_log import EntryExitLog
from app.models.parking_session import ParkingSession
from app.services.slot_recovery_service import (
    RECOVERY_CAMERA_ID,
    RecoveryRejected,
    recover_slot_session,
)

OBSERVED = datetime(2026, 7, 30, 16, 0, 0)
engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    s = TestSession()
    # parking_slots is VA-owned; PMS-AI has no model for it and only reads two
    # columns, so the guard uses raw SQL. Mirror the shape here.
    s.execute(text("CREATE TABLE IF NOT EXISTS parking_slots ("
                   "slot_id VARCHAR(50) PRIMARY KEY, is_available INT, "
                   "current_plate VARCHAR(50))"))
    s.commit()
    try:
        yield s
    finally:
        s.execute(text("DROP TABLE IF EXISTS parking_slots"))
        s.commit()
        s.close()
        Base.metadata.drop_all(bind=engine)


def _slot(db, slot_id="B13 COO", *, occupied=True, plate="BHD-9990"):
    db.execute(text("DELETE FROM parking_slots WHERE slot_id=:s"), {"s": slot_id})
    db.execute(
        text("INSERT INTO parking_slots (slot_id,is_available,current_plate) "
             "VALUES (:s,:a,:p)"),
        {"s": slot_id, "a": 0 if occupied else 1, "p": plate},
    )
    db.flush()


def _recover(db, plate="BHD-9990", slot_id="B13 COO"):
    return recover_slot_session(
        db, plate_number=plate, slot_id=slot_id, camera_id="CAM-24",
        reid_score=0.87, reid_margin=0.23, reid_same_view=True,
        ocr_text="9990BHD", observed_at=OBSERVED,
    )


# ── the race guard ───────────────────────────────────────────────────────────


def test_refuses_when_the_slot_went_vacant(db):
    """The car left between VA observing and this landing."""
    _slot(db, occupied=False)
    with pytest.raises(RecoveryRejected, match="VACANT"):
        _recover(db)
    assert db.query(ParkingSession).count() == 0


def test_refuses_when_another_car_took_the_slot(db):
    _slot(db, plate="RGR-6466")
    with pytest.raises(RecoveryRejected, match="another car"):
        _recover(db, plate="BHD-9990")
    assert db.query(ParkingSession).count() == 0


def test_refuses_when_the_slot_plate_was_cleared(db):
    """Occupied, but VA no longer claims to know who — evidence is stale."""
    _slot(db, plate="")
    with pytest.raises(RecoveryRejected, match="cleared"):
        _recover(db)
    assert db.query(ParkingSession).count() == 0


def test_refuses_for_an_unknown_slot(db):
    with pytest.raises(RecoveryRejected, match="does not exist"):
        _recover(db, slot_id="NO-SUCH-SLOT")


def test_guard_reads_live_state_not_the_request(db):
    """The claim is re-checked at write time, not taken on trust.

    This is the whole mechanism: the request still says BHD-9990/B13, and it is
    the DB moving underneath it that must decide the outcome.
    """
    _slot(db)
    _recover(db)                       # succeeds against the state as it stands
    db.query(ParkingSession).delete()
    _slot(db, plate="RGR-6466")        # the world moves
    with pytest.raises(RecoveryRejected):
        _recover(db)                   # identical request, now refused


# ── plate spelling ───────────────────────────────────────────────────────────


def test_accepts_the_digits_first_spelling(db):
    """Slot OCR emits `9990BHD` for `BHD-9990`; both name the same car."""
    _slot(db, plate="9990BHD")
    _session, created = _recover(db, plate="BHD-9990")
    assert created


def test_still_rejects_a_genuinely_different_plate(db):
    """Tolerating spelling must not tolerate a different car."""
    _slot(db, plate="BHD-9909")
    with pytest.raises(RecoveryRejected):
        _recover(db, plate="BHD-9990")


# ── the write ────────────────────────────────────────────────────────────────


def test_opens_a_session_bound_to_the_slot(db):
    _slot(db)
    session, created = _recover(db)
    db.flush()
    assert created
    assert session.status == "open"
    assert session.plate_number == "BHD-9990"
    assert session.slot_id == "B13 COO"
    assert session.slot_camera_id == "CAM-24"
    assert session.entry_time == OBSERVED
    # Never mistakable for a car that came through the barrier.
    assert session.entry_camera_id == RECOVERY_CAMERA_ID


def test_writes_a_gate_log_so_the_reconciler_sees_the_pass(db):
    """Without this the HikCentral sweep could later open a second session."""
    _slot(db)
    _recover(db)
    db.flush()
    row = db.query(EntryExitLog).one()
    assert (row.plate_number, row.gate) == ("BHD-9990", "entry")
    assert row.camera_id == RECOVERY_CAMERA_ID


def test_is_idempotent_for_a_car_already_inside(db):
    """A retried or duplicated request must not open a second session."""
    _slot(db)
    first, created_first = _recover(db)
    db.flush()
    second, created_second = _recover(db)
    db.flush()
    assert created_first and not created_second
    assert first.id == second.id
    assert db.query(ParkingSession).count() == 1


def test_a_car_with_an_existing_session_needs_no_recovery(db):
    _slot(db)
    now = datetime(2026, 7, 30, 6, 0, 0)
    db.add(ParkingSession(
        plate_number="BHD-9990", entry_time=now, entry_camera_id="CAM-ENTRY",
        status="open", created_at=now, updated_at=now,
    ))
    db.flush()
    _session, created = _recover(db)
    assert not created
    assert db.query(ParkingSession).count() == 1


def test_disabled_by_default():
    assert settings.SLOT_RECOVERY_ENABLED is False, (
        "recovery creates sessions from evidence that never passed the gate — "
        "it must be opt-in"
    )
