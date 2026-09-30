"""Stage 3 — one pool of open stays, and an exact match that actually matches.

Numbered to the Stage 3 scenario document so each test can be read against it.

Three defects lived in the gap between `close_session` and the matcher:

  * they asked DIFFERENT questions — the close unbounded, the matcher bounded to
    `EXIT_MATCH_MAX_AGE_HOURS=72` — so `ABR-8000` (98h) and `KBD-6795` (120h)
    existed for one and not the other;
  * "exact" meant string equality, and `normalize_plate` passes a dashed read
    through unchanged, so a digits-first `6707-SDD` never equalled the stored
    `SDD-6707` and fell through to the fuzzy tier for a plate that was right;
  * the audit pairing was looked up by the EXIT's plate, which is the one string
    that does not match when the entry was misread.

Stage 3 owns the EXACT match and nothing else. Since 2026-08-18 no string rule
may close a session — two real cars can differ by one letter or digit — so every
non-exact case here must end with the session still open.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.entry_exit_log import EntryExitLog
from app.models.parking_session import ParkingSession
from app.services import (
    entry_exit_service,
    exit_match_service,
    exit_pipeline,
    hikcentral,
    parking_session_service as pss,
)
from app.services.event_parser import ParsedCameraEvent
from app.services.hikcentral.models import HikImages, VehicleLogRecord

FTZ = timezone(timedelta(hours=3))
ENTRY = datetime(2026, 8, 16, 7, 0, 0)
EXIT = datetime(2026, 8, 16, 9, 30, 0)

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


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0)
    monkeypatch.setattr(
        settings, "CAMERAS",
        {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}},
    )
    monkeypatch.setattr(
        "app.utils.core_backend_client.notify_pms_anpr", AsyncMock()
    )
    monkeypatch.setattr(
        "app.utils.va_reid_client.compare", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.utils.va_reid_client.rename", AsyncMock(return_value=True)
    )


def _stay(db, plate, entry_time=ENTRY, **kw):
    row = ParkingSession(
        plate_number=plate, entry_time=entry_time, entry_camera_id="CAM-ENTRY",
        status="open", created_at=entry_time, updated_at=entry_time, **kw
    )
    db.add(row)
    db.add(EntryExitLog(
        plate_number=plate, gate="entry", event_time=entry_time,
        camera_id="CAM-ENTRY",
    ))
    db.flush()
    return row


def _exit_event(plate, at=EXIT):
    return ParsedCameraEvent(
        camera_id="CAM-EXIT", device_serial="S", channel_id=1,
        event_type="AccessControllerEvent", detection_target="vehicle",
        region_id="exit", channel_name="Exit", trigger_time=at.replace(tzinfo=FTZ),
        raw_xml="{}", plate_number=plate, gate="exit",
        snapshot_path="/snap/exit.jpg",
    )


def _close(db, plate, at=EXIT):
    return pss.close_session(
        db, plate_number=plate, event_time=at,
        camera_id="CAM-EXIT", snapshot_path=None,
    )


# ══════════════════════════════════════════════════════════════════════════
# 1. Candidate pool and normalization
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "read", ["6707-SDD", "6707 SDD", "SDD 6707", "sdd-6707", "SDD6707"]
)
def test_1_1_format_agnostic_exact_match(db, read):
    """Every spelling of one plate must close its stay on the EXACT tier.

    `normalize_plate` passes any dashed read through unchanged, so string
    equality saw `6707-SDD` and stored `SDD-6707` as two different cars and sent
    a plate that was exactly right down to the fuzzy tier. `plate_parts` compares
    the letter set and the digit set, which is order- and separator-blind.
    """
    stay = _stay(db, "SDD-6707")

    closed = _close(db, read)

    assert closed is not None and closed.id == stay.id
    assert closed.status == "closed"
    # It must land on the exact tier, not be rescued by the matcher afterwards.
    assert closed.exit_camera_id == "CAM-EXIT"


def test_1_1b_a_near_plate_is_still_a_different_car(db):
    """Order-independence must not become permissiveness. One digit apart is
    two cars, and the exact tier says so."""
    _stay(db, "SDD-6707")

    assert _close(db, "SDD-6708") is None
    assert db.query(ParkingSession).one().status == "open"


def test_1_2_a_120h_stay_is_reachable_and_closes(db):
    """`EXIT_MATCH_MAX_AGE_HOURS=72` bounded the matcher while the close had no
    bound, so the two halves of one decision disagreed about which stays exist —
    and the stays that most needed resolving (`KBD-6795` 120h, `ABR-8000` 98h)
    were exactly the ones hidden."""
    stay = _stay(db, "KBD-6795", entry_time=EXIT - timedelta(hours=120))

    assert [row.plate_number for row in pss.open_stays(db, EXIT)] == ["KBD-6795"]

    closed = _close(db, "KBD-6795")
    assert closed is not None and closed.id == stay.id
    assert closed.duration_seconds == 120 * 3600


def test_1_3_clock_skew_between_the_two_cameras_is_tolerated(db):
    """Entry time comes from the entry camera's clock, exit time from the exit
    camera's, so a car leaving within the drift of arriving must still find its
    own stay.

    20s, derived: `HIK_MATCH_MAX_SKEW_SECONDS=10` refuses to pair a HikCentral
    record with a gate event more than 10s apart and all 129 entry validations in
    ai-logs.txt passed it, so each camera is within 10s of the platform and two
    are within 20s of each other.
    """
    stay = _stay(db, "SKEW-1", entry_time=EXIT + timedelta(seconds=5))

    closed = _close(db, "SKEW-1")

    assert closed is not None and closed.id == stay.id
    assert closed.duration_seconds == 0, "a negative stay is clamped, not stored"


def test_1_3b_the_tolerance_is_not_a_short_stay_allowance(db):
    """Why 20s and not 120s. A car that exits and RE-ENTERS has two stays; the
    exit belongs to the old one. A late exit reaching the NEW stay would close a
    car sitting in the garage — and its plate matches EXACTLY, so nothing
    downstream would question it.

    The shortest real stay measured over 8/10-8/16 is 248s and not one of the 124
    is under 120s, so widening this buys nothing and only widens that hole.
    """
    _stay(db, "BACK-1", entry_time=EXIT + timedelta(minutes=2))

    assert pss.open_stays(db, EXIT) == []
    assert _close(db, "BACK-1") is None


# ══════════════════════════════════════════════════════════════════════════
# 2. Strict non-exact rules — Stage 3 closes nothing it is not certain of
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("read", ["ABC-1235", "ABC-123"])
def test_2_1_a_one_character_misread_is_handed_on_not_closed(db, read):
    """Substitution and truncation both. The stay survives Stage 3 untouched and
    arrives at the evidence tiers as a candidate carrying its diagnostics."""
    _stay(db, "ABC-1234")

    assert _close(db, read) is None
    assert db.query(ParkingSession).one().status == "open"

    resolution = exit_match_service.resolve_unmatched_exit(db, read, EXIT)
    assert not resolution.matched
    assert resolution.session is None
    candidate = next(c for c in resolution.candidates if c.plate == "ABC-1234")
    assert candidate.distance == 1, (
        "the metric survives as a DIAGNOSTIC printed in the Log X line"
    )


def test_2_2_a_shared_digit_group_alone_closes_nothing(db):
    """`AAA-2538` -> `KXR-2538` on 8/11 and 8/12 is the whole observed history of
    the digit-group rule: one car, three letters wrong, digits unique that day by
    coincidence. Two REAL cars can share a digit group."""
    _stay(db, "KXR-2538")

    assert _close(db, "AAA-2538") is None
    assert db.query(ParkingSession).one().status == "open"

    resolution = exit_match_service.resolve_unmatched_exit(db, "AAA-2538", EXIT)
    assert not resolution.matched
    assert [c.plate for c in resolution.candidates] == ["KXR-2538"]


# ══════════════════════════════════════════════════════════════════════════
# 3. Ingest equivalence and the re-entry-reconciled branch
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_1_edge_and_hik_recovered_exits_resolve_identically(db, monkeypatch):
    """Two ways of noticing a car leaving, one pipeline. The only things allowed
    to differ are which camera the row names and which GUID it consumed."""
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT)
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    edge_stay = _stay(db, "EGY-1111")
    hik_stay = _stay(db, "EGY-2222")

    await entry_exit_service.handle_anpr_event(_exit_event("EGY-1111"), db)
    db.commit()

    async def fake_list(resource_ids, begin, end, db):
        return [VehicleLogRecord.from_openapi_record({
            "crossRecordSyscode": "G-EQ", "cameraIndexCode": "510",
            "plateNo": "2222EGY", "crossTime": EXIT.replace(tzinfo=FTZ).isoformat(),
            "vehiclePicUri": "Vsm://v",
        })]

    async def fake_images(outcome):
        return HikImages()

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)
    monkeypatch.setattr(hikcentral, "download_hik_images", fake_images)

    await entry_exit_service._reconcile_missed_exits(
        db, window=(EXIT - timedelta(hours=1), EXIT)
    )
    db.commit()
    db.refresh(edge_stay)
    db.refresh(hik_stay)

    assert edge_stay.status == hik_stay.status == "closed"
    assert edge_stay.exit_time == hik_stay.exit_time == EXIT
    assert edge_stay.duration_seconds == hik_stay.duration_seconds == 9000
    assert edge_stay.exit_camera_id == "CAM-EXIT"
    assert hik_stay.exit_camera_id == exit_pipeline.RECONCILE_CAMERA_ID
    # The Hik record spells the plate digits-first; both closed the same way.
    assert hik_stay.plate_number == "EGY-2222"


@pytest.mark.asyncio
async def test_3_1b_both_paths_build_the_same_exit_event(db):
    """The structural half of equivalence: one car, one description of it."""
    edge = await exit_pipeline.from_camera_event(
        _exit_event("EGY-1111"), "EGY-1111", EXIT, db
    )
    polled = exit_pipeline.from_polled_outcome(
        hikcentral.polled_outcome(VehicleLogRecord.from_openapi_record({
            "crossRecordSyscode": "G-1", "cameraIndexCode": "510",
            "plateNo": "1111EGY", "crossTime": EXIT.replace(tzinfo=FTZ).isoformat(),
            "vehiclePicUri": "Vsm://v",
        })),
        "/snap/exit.jpg",
    )

    assert edge.plate == polled.plate == "EGY-1111"
    assert edge.event_time == polled.event_time == EXIT
    assert edge.snapshot_path == polled.snapshot_path
    assert edge.source != polled.source, "only the provenance differs"


def test_3_2_a_delayed_real_exit_corrects_a_reentry_bound(db):
    """`close_session:210-236` handles a real exit arriving after a validated
    re-entry already closed the previous stay at an INFERRED upper bound.

    The refactor guards every close on the state it expects to find, and this
    stay is deliberately `closed` — so it needs its own expected state or the
    correction is refused as a double-close.
    """
    stay = _stay(db, "LATE-2", entry_time=ENTRY)
    stay.status = "closed"
    stay.exit_camera_id = pss.REENTRY_RECONCILIATION_CAMERA_ID
    stay.exit_time = EXIT + timedelta(hours=1)
    stay.duration_seconds = 12600
    db.flush()

    closed = _close(db, "LATE-2")

    assert closed is not None and closed.id == stay.id
    assert closed.exit_time == EXIT, "the inferred bound must be replaced"
    assert closed.exit_camera_id == "CAM-EXIT"
    assert closed.duration_seconds == 9000


def test_3_2b_a_reentry_bound_that_does_not_contain_this_exit_is_left_alone(db):
    """The correction is for an exit INSIDE the inferred interval. One outside it
    belongs to a different stay, and rewriting this one would move a boundary
    that was inferred from real evidence."""
    stay = _stay(db, "LATE-3", entry_time=ENTRY)
    stay.status = "closed"
    stay.exit_camera_id = pss.REENTRY_RECONCILIATION_CAMERA_ID
    stay.exit_time = EXIT - timedelta(hours=1)
    db.flush()

    assert _close(db, "LATE-3") is None
    db.refresh(stay)
    assert stay.exit_time == EXIT - timedelta(hours=1)


# ══════════════════════════════════════════════════════════════════════════
# 4. Edge cases, audit trail, database safety
# ══════════════════════════════════════════════════════════════════════════


def test_4_1_two_open_stays_on_one_plate_name_the_stranded_one(db, caplog):
    """Two open stays under one plate means an exit was MISSED: the car came back
    and its previous stay was never closed.

    The newest is what this exit ends. The older is a stranded stay that needs
    its own answer, so it is NAMED in the log rather than swallowed by a
    `.first()` — and no ORM call may raise on the multiplicity.
    """
    older = _stay(db, "DUP-9999", entry_time=ENTRY - timedelta(hours=5))
    newer = _stay(db, "DUP-9999", entry_time=ENTRY)

    with caplog.at_level("WARNING"):
        closed = _close(db, "DUP-9999")

    assert closed is not None and closed.id == newer.id
    db.refresh(older)
    assert older.status == "open", "the stranded stay must not be silently closed"
    assert any(
        "STRANDED" in r.message and f"id={older.id}" in r.message
        for r in caplog.records
    ), "the duplicate must be surfaced, not swallowed"


@pytest.mark.asyncio
async def test_4_2_the_audit_row_is_paired_from_the_resolved_session(db, monkeypatch):
    """`EntryExitLog.matched_entry_id` comes from the stay that was actually
    closed, both ways, so the audit trail and the session cannot disagree."""
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT)
    stay = _stay(db, "XYZ-5555")

    await entry_exit_service.handle_anpr_event(_exit_event("XYZ-5555"), db)
    db.commit()

    db.refresh(stay)
    exit_row = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    entry_row = db.query(EntryExitLog).filter(EntryExitLog.gate == "entry").one()

    assert stay.status == "closed"
    assert exit_row.matched_entry_id == entry_row.id
    assert entry_row.matched_entry_id == exit_row.id
    assert exit_row.parking_duration == stay.duration_seconds == 9000


@pytest.mark.asyncio
async def test_4_2b_the_pairing_survives_a_corrected_plate(db, monkeypatch):
    """The case the old code got wrong. UC2 searched for an entry row under the
    EXIT's plate — the one string that does not match when the entry was misread
    — so every non-exact close left `matched_entry_id` NULL."""
    async def compare(image_path, plates):
        return {"query_quality_ok": True,
                "results": [{"plate": "ABC-123", "score": 0.9}]}

    monkeypatch.setattr("app.utils.va_reid_client.compare", compare)
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT)
    _stay(db, "ABC-123")

    await entry_exit_service.handle_anpr_event(_exit_event("ABC-1234"), db)
    db.commit()

    exit_row = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    entry_row = db.query(EntryExitLog).filter(EntryExitLog.gate == "entry").one()
    assert exit_row.matched_entry_id == entry_row.id
    assert entry_row.matched_entry_id == exit_row.id


def test_4_3_only_one_of_two_concurrent_workers_closes_the_stay(tmp_path):
    """Two workers, two TRANSACTIONS, one stay.

    Not a mocked race: a file-backed database and two independent sessions, which
    is the shape the edge webhook and the reconcile sweep actually have — both
    run the full pipeline since Stage 1, so both can reach one stay.

    The guard is the UPDATE's own `WHERE status = 'open'`, so the database
    decides. The loser must get a clean None, not an exception, and the winner's
    exit_time must survive.
    """
    url = f"sqlite:///{tmp_path / 'race.db'}"
    race_engine = create_engine(url)
    Base.metadata.create_all(bind=race_engine)
    Maker = sessionmaker(autocommit=False, autoflush=False, bind=race_engine)

    seed = Maker()
    _stay(seed, "SESS-200")
    seed.commit()
    seed.close()

    worker_a, worker_b = Maker(), Maker()
    try:
        # Both read the stay as open before either writes — the stale-read window.
        a_pool = pss.open_stays(worker_a, EXIT)
        b_pool = pss.open_stays(worker_b, EXIT)
        assert len(a_pool) == len(b_pool) == 1

        closed_a = pss.close_session(
            worker_a, plate_number="SESS-200", event_time=EXIT,
            camera_id="CAM-EXIT", snapshot_path=None,
        )
        worker_a.commit()

        closed_b = pss.close_matched_session(
            worker_b, b_pool[0], exit_time=EXIT + timedelta(hours=1),
            camera_id="CAM-EXIT", snapshot_path=None,
        )
        worker_b.commit()

        assert closed_a is not None, "exactly one worker must win"
        assert closed_b is None, "the loser must get None, not an exception"

        check = Maker()
        winner = check.query(ParkingSession).one()
        assert winner.status == "closed"
        assert winner.exit_time == EXIT, "the winner's close must stand"
        check.close()
    finally:
        worker_a.close()
        worker_b.close()
        Base.metadata.drop_all(bind=race_engine)


def test_4_3b_the_loser_says_why_it_refused(tmp_path, caplog):
    """A refusal that leaves no trace is indistinguishable from a bug. The log
    must name the stay and the state it expected to find."""
    url = f"sqlite:///{tmp_path / 'race2.db'}"
    race_engine = create_engine(url)
    Base.metadata.create_all(bind=race_engine)
    Maker = sessionmaker(autocommit=False, autoflush=False, bind=race_engine)

    seed = Maker()
    _stay(seed, "SESS-201")
    seed.commit()
    seed.close()

    winner, loser = Maker(), Maker()
    try:
        stale = pss.open_stays(loser, EXIT)[0]
        pss.close_session(
            winner, plate_number="SESS-201", event_time=EXIT,
            camera_id="CAM-EXIT", snapshot_path=None,
        )
        winner.commit()

        with caplog.at_level("WARNING"):
            assert pss.close_matched_session(
                loser, stale, exit_time=EXIT, camera_id="CAM-EXIT",
                snapshot_path=None,
            ) is None

        assert any(
            "refused to close" in r.message and "SESS-201" in r.message
            for r in caplog.records
        )
    finally:
        winner.close()
        loser.close()
        Base.metadata.drop_all(bind=race_engine)
