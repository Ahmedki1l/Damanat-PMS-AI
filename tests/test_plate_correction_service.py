"""Rewriting a stay that was opened under a misread plate.

Only the exit can correct an entry: all 144 entry bursts in ai-logs.txt had
`reads=1` with nothing discarded, and HikCentral is fed by the same entry LPR, so
there are zero `plate_corrected` lines on the entry side anywhere in the window.
The exit read is the first independent look at the car.

The invariant these guard: the misread is never destroyed, and the correction is
all-or-nothing. A ledger row without a session rewrite is worse than no
correction at all — it claims a fix that did not happen.
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.entry_exit_log import EntryExitLog
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import plate_correction_service as pcs

ENTRY_TIME = datetime(2026, 8, 16, 7, 0, 0)
EXIT_TIME = datetime(2026, 8, 16, 9, 0, 0)

engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _stay(db, plate, exit_time=EXIT_TIME):
    row = ParkingSession(
        plate_number=plate,
        entry_time=ENTRY_TIME,
        entry_camera_id="CAM-ENTRY",
        exit_time=exit_time,
        status="closed",
        created_at=ENTRY_TIME,
        updated_at=ENTRY_TIME,
    )
    db.add(row)
    db.flush()
    return row


def _log(db, plate, gate, when):
    row = EntryExitLog(
        plate_number=plate, gate=gate, event_time=when, camera_id="CAM-" + gate.upper()
    )
    db.add(row)
    db.flush()
    return row


# ── the misread must survive the rewrite ────────────────────────────────────


def test_the_misread_is_recoverable_from_the_ledger(db):
    """`close_matched_session` used to keep the misread by leaving it ON the
    session, which showed a car that was not there. It is still the only evidence
    that can later prove this match right or wrong — it just lives in the ledger
    now."""
    stay = _stay(db, "SNP-226")

    correction = pcs.apply_correction(db, stay, "SNA-226", "digit group '226'")
    db.flush()

    assert correction is not None and correction.applied
    assert stay.plate_number == "SNA-226"
    row = db.query(HikValidation).one()
    assert row.reported_plate == "SNP-226", "the misread must be recoverable"
    assert row.canonical_plate == "SNA-226"
    assert row.session_id == stay.id
    assert row.match_reason == "digit group '226'", (
        "the ledger must say WHY the car was renamed, not just that it was"
    )


def test_the_entry_row_follows_the_session(db):
    """The stay and the row that opened it must not disagree — a corrected
    session paired with an entry row still holding the misread reads as two
    different cars."""
    stay = _stay(db, "SNP-226")
    entry = _log(db, "SNP-226", "entry", ENTRY_TIME)
    exit_row = _log(db, "SNA-226", "exit", EXIT_TIME)

    pcs.apply_correction(db, stay, "SNA-226", "digits", exit_log=exit_row)
    db.flush()

    db.refresh(entry)
    assert entry.plate_number == "SNA-226"
    assert db.query(HikValidation).one().entry_exit_log_id == exit_row.id


def test_nothing_happens_when_the_plate_is_already_right(db):
    stay = _stay(db, "SNA-226")
    assert pcs.apply_correction(db, stay, "SNA-226", "noop") is None
    assert db.query(HikValidation).count() == 0


# ── the ledger's identity ───────────────────────────────────────────────────


def test_synthetic_guids_never_collide_with_hikcentral_ones(db):
    """`guid` is UNIQUE and normally HikCentral's identity for one pass.
    HikCentral emits bare 32-char hex, so the namespace prefix is unreachable
    for it — and anything reading the ledger can tell "the platform said so"
    from "our own matcher said so"."""
    stay = _stay(db, "SNP-226")
    pcs.apply_correction(db, stay, "SNA-226", "digits")
    db.flush()

    guid = db.query(HikValidation).one().guid
    assert guid.startswith(pcs.LOCAL_GUID_PREFIX)
    assert not all(c in "0123456789ABCDEFabcdef" for c in guid)


def test_a_hikcentral_backed_correction_keeps_its_real_guid(db):
    """When the platform proved the plate, the ledger row IS that pass — reusing
    its GUID is also what stops the reconcile sweep redoing the same exit."""
    stay = _stay(db, "SNP-226")
    pcs.apply_correction(db, stay, "SNA-226", "plate_corrected", hik_guid="ABC123")
    db.flush()

    assert db.query(HikValidation).one().guid == "ABC123"


def test_a_replayed_correction_is_not_applied_twice(db):
    """The ledger's uniqueness is what makes a replay detectable."""
    stay = _stay(db, "SNP-226")
    pcs.apply_correction(db, stay, "SNA-226", "digits", hik_guid="ABC123")
    db.flush()

    stay.plate_number = "SNP-226"  # pretend the first attempt was rolled back
    assert pcs.apply_correction(db, stay, "SNA-226", "digits", hik_guid="ABC123") is None
    assert db.query(HikValidation).count() == 1


# ── vehicles.plate_number is UNIQUE ─────────────────────────────────────────


def test_the_placeholder_is_renamed_when_the_correct_plate_is_free(db):
    """Renaming in place means anything already pointing at the placeholder
    follows the correction instead of being orphaned."""
    db.add(Vehicle(
        plate_number="SNP-226", owner_name="Unknown", title="Unknown",
        vehicle_type="unknown", is_registered=False,
    ))
    db.flush()
    stay = _stay(db, "SNP-226")

    correction = pcs.apply_correction(db, stay, "SNA-226", "digits")
    db.flush()

    assert correction.vehicle_merged is False
    assert db.query(Vehicle).count() == 1
    assert db.query(Vehicle).one().plate_number == "SNA-226"


def test_an_existing_correct_row_wins_and_the_placeholder_is_left_alone(db):
    """`plate_number` is UNIQUE, so the placeholder cannot be renamed onto a row
    that already exists — and it is not deleted either: rows outside this stay
    may still reference it, and a dangling FK is worse than an orphan."""
    db.add(Vehicle(
        plate_number="SNP-226", owner_name="Unknown", title="Unknown",
        vehicle_type="unknown", is_registered=False,
    ))
    db.add(Vehicle(
        plate_number="SNA-226", owner_name="Real Owner", title="Mr",
        vehicle_type="sedan", is_registered=True,
    ))
    db.flush()
    stay = _stay(db, "SNP-226")

    correction = pcs.apply_correction(db, stay, "SNA-226", "digits")
    db.flush()

    assert correction.vehicle_merged is True
    assert db.query(Vehicle).count() == 2
    assert stay.vehicle_id == (
        db.query(Vehicle).filter(Vehicle.plate_number == "SNA-226").one().id
    )


def test_a_registered_vehicle_is_never_renamed(db):
    """A human vouched for that plate, which outranks a matcher."""
    db.add(Vehicle(
        plate_number="SNP-226", owner_name="Real Owner", title="Mr",
        vehicle_type="sedan", is_registered=True,
    ))
    db.flush()
    stay = _stay(db, "SNP-226")

    pcs.apply_correction(db, stay, "SNA-226", "digits")
    db.flush()

    kept = db.query(Vehicle).filter(Vehicle.plate_number == "SNP-226").one()
    assert kept.is_registered is True
    assert db.query(Vehicle).filter(Vehicle.plate_number == "SNA-226").count() == 1


# ── VA is downstream, never a dependency ────────────────────────────────────


@pytest.mark.asyncio
async def test_va_down_does_not_fail_a_correction(db, monkeypatch):
    """`apply_correction` has already committed by the time VA is told. A VA
    that is unreachable must not be able to undo it."""
    stay = _stay(db, "SNP-226")
    correction = pcs.apply_correction(db, stay, "SNA-226", "digits")
    db.commit()

    async def unreachable(*args, **kwargs):
        return False

    monkeypatch.setattr("app.utils.va_reid_client.rename", unreachable)

    assert await pcs.notify_va(correction) is False
    assert db.query(ParkingSession).one().plate_number == "SNA-226"


@pytest.mark.asyncio
async def test_va_is_told_both_plates(db, monkeypatch):
    stay = _stay(db, "SNP-226")
    correction = pcs.apply_correction(db, stay, "SNA-226", "digits")
    db.commit()

    sent = AsyncMock(return_value=True)
    monkeypatch.setattr("app.utils.va_reid_client.rename", sent)

    assert await pcs.notify_va(correction) is True
    sent.assert_awaited_once_with("SNP-226", "SNA-226")


# ── history is append-only ──────────────────────────────────────────────────


def test_the_correction_never_touches_slot_history():
    """A correction is an append-only event plus a current-state update.

    `slot_status` is VA's table — PMS-AI has no ORM model for it and should not
    grow one, so this cannot be asserted by writing a row and re-reading it.
    What it CAN assert is that no executable line of this module reaches for it,
    which fails the day someone adds a rewrite. Historical rows record what VA
    actually observed at the time; a later correction does not make that
    observation untrue.

    Docstrings and comments are stripped first — the module discusses the rule
    at length, and prose is not a rewrite.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(pcs))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)

    code = ast.unparse(tree)
    assert "slot_status" not in code
    assert "SlotStatus" not in code
