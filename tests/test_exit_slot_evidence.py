"""Stage 4 — slot evidence ahead of Re-ID, and Log X for what neither can place.

Since the string rules were removed this is the ONLY tier that can resolve an
exit whose entry plate was misread. That raises the stakes on two asymmetries,
and every test here is about one of them:

  * only slot evidence may ELIMINATE, and only on something physical — VA
    watched that car sitting in its slot while this exit happened;
  * SILENCE IS NEVER EVIDENCE AGAINST. 15 of 35 slots run VA_IDENTITY_DISABLED
    on B2 and produce no plate signal at all. Reading their silence as
    elimination would quietly make every B2 car unmatchable, which is worse than
    the ambiguity it would appear to resolve.

`parking_slots` and `slot_status` are VA-owned. PMS-AI has no model for them and
should not grow one, so these tests create them with raw DDL exactly as the
service reads them.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.parking_session import ParkingSession
from app.services import exit_match_service as ems

ENTRY = datetime(2026, 8, 16, 7, 0, 0)
EXIT = datetime(2026, 8, 16, 9, 30, 0)

engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    # VA's tables, as the raw SQL in exit_match_service expects to find them.
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS parking_slots ("
        "slot_id VARCHAR(50) PRIMARY KEY, is_available BOOLEAN, "
        "current_plate VARCHAR(50))"
    ))
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS slot_status (id INTEGER PRIMARY KEY, "
        "slot_id VARCHAR(50), plate_number VARCHAR(20), status VARCHAR(20), "
        "time DATETIME)"
    ))
    session.commit()
    try:
        yield session
    finally:
        # These are VA's tables, created here by hand, so `drop_all` (which only
        # knows PMS-AI's metadata) will not clear them between tests.
        session.rollback()
        session.execute(text("DELETE FROM parking_slots"))
        session.execute(text("DELETE FROM slot_status"))
        session.commit()
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def stage4(monkeypatch):
    monkeypatch.setattr(settings, "EXIT_SLOT_EVIDENCE_ENABLED", True)
    monkeypatch.setattr(settings, "EXIT_DRIVE_OUT_SECONDS", 300.0)
    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", False)


def _stay(db, plate, slot_id=None, entry_time=ENTRY):
    row = ParkingSession(
        plate_number=plate, entry_time=entry_time, entry_camera_id="CAM-ENTRY",
        status="open", slot_id=slot_id, created_at=entry_time,
        updated_at=entry_time,
    )
    db.add(row)
    db.flush()
    return row


def _slot(db, slot_id, occupied, plate=None):
    db.execute(
        text("INSERT INTO parking_slots (slot_id, is_available, current_plate) "
             "VALUES (:s, :a, :p)"),
        {"s": slot_id, "a": not occupied, "p": plate},
    )
    db.flush()


def _transition(db, slot_id, status, when, plate=None):
    db.execute(
        text("INSERT INTO slot_status (slot_id, plate_number, status, time) "
             "VALUES (:s, :p, :st, :t)"),
        {"s": slot_id, "p": plate, "st": status, "t": when},
    )
    db.flush()


def _verdicts(db, plate="EXIT-1", at=EXIT):
    resolution = ems.resolve_unmatched_exit(db, plate, at)
    return ems.slot_evidence(db, resolution.candidates, at), resolution


# ── elimination: the physical rule ──────────────────────────────────────────


def test_a_car_still_in_its_slot_is_eliminated(db):
    """The one thing slot evidence may do unilaterally. VA is watching this car
    sit in B6 while somebody drives out of the gate — it is not that somebody."""
    _stay(db, "PARKED-1", slot_id="B6")
    _slot(db, "B6", occupied=True, plate="PARKED-1")

    verdicts, _ = _verdicts(db)

    assert verdicts["PARKED-1"].kind == ems.ELIMINATED
    assert "B6" in verdicts["PARKED-1"].reason


@pytest.mark.asyncio
async def test_every_candidate_still_parked_leaves_nothing_to_match(db):
    _stay(db, "PARKED-1", slot_id="B6")
    _slot(db, "B6", occupied=True, plate="PARKED-1")

    out = await ems.resolve_with_appearance(db, "EXIT-1", EXIT, None, "crop.jpg")

    assert out.kind == ems.NO_CANDIDATES
    assert out.session is None
    assert "still parked" in out.reason


# ── confirmation: a slot that emptied while this car was leaving ────────────


@pytest.mark.asyncio
async def test_a_unique_vacancy_confirms_without_reid(db, monkeypatch):
    """A slot that emptied inside the drive-out window is the strongest evidence
    in the system, and it closes on its own — Re-ID is not consulted."""
    stay = _stay(db, "GONE-1", slot_id="B7")
    _slot(db, "B7", occupied=False)
    _transition(db, "B7", "available", EXIT - timedelta(seconds=90))

    async def explode(image_path, plates):  # pragma: no cover
        raise AssertionError("Re-ID must not run once a slot confirmed uniquely")

    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", True)
    monkeypatch.setattr("app.utils.va_reid_client.compare", explode)

    out = await ems.resolve_with_appearance(db, "EXIT-1", EXIT, None, "crop.jpg")

    assert out.matched
    assert out.session.id == stay.id
    assert out.reason.startswith("slot evidence:")


@pytest.mark.asyncio
async def test_a_vacancy_outside_the_window_confirms_nothing(db):
    """`EXIT_DRIVE_OUT_SECONDS` is how long a car may take to reach the gate.
    A slot that emptied an hour ago is a different departure."""
    _stay(db, "GONE-1", slot_id="B7")
    _slot(db, "B7", occupied=False)
    _transition(db, "B7", "available", EXIT - timedelta(hours=1))

    verdicts, _ = _verdicts(db)

    assert verdicts["GONE-1"].kind == ems.UNKNOWN


@pytest.mark.asyncio
async def test_two_vacancies_in_the_window_decline_rather_than_guess(db):
    """Two slots emptied while one car left. One of them belongs to this exit and
    nothing here says which — so the tier hands both on instead of picking."""
    _stay(db, "GONE-1", slot_id="B7")
    _stay(db, "GONE-2", slot_id="B8")
    _slot(db, "B7", occupied=False)
    _slot(db, "B8", occupied=False)
    _transition(db, "B7", "available", EXIT - timedelta(seconds=60))
    _transition(db, "B8", "available", EXIT - timedelta(seconds=120))

    out = await ems.resolve_with_appearance(db, "EXIT-1", EXIT, None, None)

    assert not out.matched
    assert {c.plate for c in out.candidates} == {"GONE-1", "GONE-2"}


# ── the reassignment bound is not a verdict ─────────────────────────────────


def test_a_reassigned_slot_is_a_bound_not_a_verdict(db):
    """`B6_Reserved` went from `SDD-6707` to `ABR-8000`. That proves SDD-6707
    left by the reassignment — it does NOT prove it left at this exit, because a
    car can move between slots. Recorded, never acted on."""
    _stay(db, "SDD-6707", slot_id="B6")
    _slot(db, "B6", occupied=True, plate="ABR-8000")

    verdicts, _ = _verdicts(db)

    assert verdicts["SDD-6707"].kind == ems.UNKNOWN
    assert "ABR-8000" in verdicts["SDD-6707"].reason
    assert "bound not a verdict" in verdicts["SDD-6707"].reason


# ── silence is never evidence against ───────────────────────────────────────


@pytest.mark.asyncio
async def test_a_stay_with_no_slot_is_never_eliminated(db, monkeypatch):
    """B2 runs VA_IDENTITY_DISABLED: 15 of 35 slots produce no plate signal at
    all. Treating that silence as elimination would make every B2 car
    unmatchable, which is a worse failure than the ambiguity it hides.

    Asserted through the whole tier, not just the verdict dict. The verdict being
    UNKNOWN is necessary but not sufficient — what matters is that an UNKNOWN
    candidate still REACHES appearance, which is the only thing left that can
    resolve it.
    """
    _stay(db, "NOSLOT-1", slot_id=None)

    verdicts, _ = _verdicts(db)
    assert verdicts["NOSLOT-1"].kind == ems.UNKNOWN

    asked = []

    async def compare(image_path, plates):
        asked.append(list(plates))
        return None

    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", True)
    monkeypatch.setattr("app.utils.va_reid_client.compare", compare)

    out = await ems.resolve_with_appearance(db, "EXIT-1", EXIT, None, "crop.jpg")

    assert asked == [["NOSLOT-1"]], (
        "a B2 car with no slot signal must still reach appearance"
    )
    assert out.kind != ems.NO_CANDIDATES


def test_a_slot_that_does_not_exist_is_unknown_not_eliminated(db):
    _stay(db, "GHOST-1", slot_id="NOPE")

    verdicts, _ = _verdicts(db)

    assert verdicts["GHOST-1"].kind == ems.UNKNOWN
    assert "not found" in verdicts["GHOST-1"].reason


def test_unreadable_slot_tables_degrade_to_unknown(db, monkeypatch):
    """VA's tables belong to another service. An exit must not fail because a
    query against them did — and it must not silently eliminate everyone either.
    """
    _stay(db, "ANY-1", slot_id="B6")
    _slot(db, "B6", occupied=True, plate="ANY-1")

    def boom(*args, **kwargs):
        raise RuntimeError("parking_slots is gone")

    monkeypatch.setattr(ems, "_slot_rows", boom)

    verdicts, _ = _verdicts(db)

    assert verdicts["ANY-1"].kind == ems.UNKNOWN
    assert "unreadable" in verdicts["ANY-1"].reason


def test_the_tier_can_be_disarmed_without_a_redeploy(db, monkeypatch):
    """`EXIT_DRIVE_OUT_SECONDS` starts unmeasured, so the tier it feeds has an
    off switch — and off must mean UNKNOWN for everyone, not eliminated."""
    monkeypatch.setattr(settings, "EXIT_SLOT_EVIDENCE_ENABLED", False)
    _stay(db, "PARKED-1", slot_id="B6")
    _slot(db, "B6", occupied=True, plate="PARKED-1")

    verdicts, _ = _verdicts(db)

    assert verdicts["PARKED-1"].kind == ems.UNKNOWN
    assert "disabled" in verdicts["PARKED-1"].reason


# ── Re-ID sees the survivors, and Log X records the rest ────────────────────


@pytest.mark.asyncio
async def test_reid_is_asked_only_about_the_survivors(db, monkeypatch):
    """The point of ordering the tiers this way: appearance is never asked to
    choose between a car that left and a car demonstrably still parked."""
    _stay(db, "PARKED-1", slot_id="B6")
    _stay(db, "GONE-1", slot_id="B7")
    _slot(db, "B6", occupied=True, plate="PARKED-1")
    _slot(db, "B7", occupied=True, plate="SOMEONE-ELSE")

    asked = []

    async def compare(image_path, plates):
        asked.append(list(plates))
        return None

    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", True)
    monkeypatch.setattr("app.utils.va_reid_client.compare", compare)

    await ems.resolve_with_appearance(db, "EXIT-1", EXIT, None, "crop.jpg")

    assert asked == [["GONE-1"]], "the parked car must never reach appearance"


@pytest.mark.asyncio
async def test_a_below_margin_score_closes_nothing_and_logs_its_candidates(
    db, monkeypatch, caplog
):
    """Log X. `EXIT_MATCH_REID_MIN_MARGIN=0.35` is the measured 100%-precision
    point; below it nothing is forced. What must survive is the RECORD — every
    candidate, its metrics and its slot verdict — or "unresolved" is
    indistinguishable from "never looked"."""
    _stay(db, "GONE-1", slot_id="B7")
    _stay(db, "GONE-2", slot_id="B8")
    _slot(db, "B7", occupied=True, plate="OTHER-1")
    _slot(db, "B8", occupied=True, plate="OTHER-2")

    async def compare(image_path, plates):
        return {"query_quality_ok": True, "results": [
            {"plate": "GONE-1", "score": 0.55},
            {"plate": "GONE-2", "score": 0.52},
        ]}

    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", True)
    monkeypatch.setattr("app.utils.va_reid_client.compare", compare)

    out = await ems.resolve_with_appearance(db, "EXIT-1", EXIT, None, "crop.jpg")

    assert not out.matched
    assert out.session is None
    assert db.query(ParkingSession).filter(
        ParkingSession.status == "open"
    ).count() == 2, "nothing may be closed on a margin of 0.03"

    line = ems.describe(out, out.slot_verdicts)
    assert "GONE-1" in line and "GONE-2" in line
    assert "slot=unknown" in line, "the Log X line must carry the slot verdict"


def test_the_shortlist_no_longer_truncates_before_appearance(db):
    """`EXIT_MATCH_SHORTLIST` was 5, ordered by edit distance — so a plate misread
    in BOTH its letters and its digits sorted to the bottom and fell off the list
    before Re-ID ever saw it, which is precisely the case Re-ID exists for. 20 is
    VA's own ceiling."""
    for index in range(12):
        _stay(db, f"CAR-{index:04d}", slot_id=None)

    resolution = ems.resolve_unmatched_exit(db, "ZZZ-9999", EXIT)

    assert settings.EXIT_MATCH_SHORTLIST >= 20
    assert len(resolution.candidates) == 12, (
        "every open stay must reach the evidence tiers"
    )
