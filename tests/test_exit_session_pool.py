"""One pool of open stays, and an exact match that actually matches.

Three defects lived in the gap between `close_session` and the matcher:

  * they asked DIFFERENT questions — the close unbounded, the matcher bounded to
    `EXIT_MATCH_MAX_AGE_HOURS=72` — so `ABR-8000` (98h) and `KBD-6795` (120h)
    existed for one and not the other;
  * "exact" meant string equality, and `normalize_plate` passes a dashed read
    through unchanged, so a digits-first `6707-SDD` never equalled the stored
    `SDD-6707` and fell through to the fuzzy tier for a plate that was right;
  * the audit pairing was looked up by the EXIT's plate, which is the one string
    that does not match when the entry was misread.
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
def quiet(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0)
    monkeypatch.setattr(
        settings, "CAMERAS",
        {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}},
    )
    monkeypatch.setattr(
        "app.utils.core_backend_client.notify_pms_anpr", AsyncMock()
    )
    monkeypatch.setattr("app.utils.va_reid_client.compare", AsyncMock(return_value=None))
    monkeypatch.setattr("app.utils.va_reid_client.rename", AsyncMock(return_value=True))


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


# ── the exact match ─────────────────────────────────────────────────────────


def test_a_digits_first_exit_closes_its_letters_first_stay(db):
    """`6707-SDD` and `SDD-6707` are one car. `normalize_plate` passes any dashed
    read through unchanged, so string equality never saw it — and a plate that
    was exactly right was handed to the fuzzy tier."""
    stay = _stay(db, "SDD-6707")

    closed = pss.close_session(
        db, plate_number="6707-SDD", event_time=EXIT,
        camera_id="CAM-EXIT", snapshot_path=None,
    )

    assert closed is not None and closed.id == stay.id
    assert closed.status == "closed"


def test_a_different_car_is_never_closed_by_a_near_plate(db):
    """The other half of order-independence: it must not become permissive.
    `(letters, digits)` equality accepts a re-spelling, never a different car."""
    _stay(db, "SDD-6707")

    assert pss.close_session(
        db, plate_number="SDD-6708", event_time=EXIT,
        camera_id="CAM-EXIT", snapshot_path=None,
    ) is None


# ── the pool ────────────────────────────────────────────────────────────────


def test_a_120h_stay_is_in_the_pool(db):
    """`KBD-6795` at 120h and `ABR-8000` at 98h were invisible to the matcher
    while being visible to the close. The stays that most need resolving were
    exactly the ones the age bound hid."""
    _stay(db, "KBD-6795", entry_time=EXIT - timedelta(hours=120))

    pool = pss.open_stays(db, EXIT)

    assert [row.plate_number for row in pool] == ["KBD-6795"]


def test_a_stay_that_starts_after_this_exit_is_not_in_the_pool(db):
    """A car cannot leave before it arrived. This is the only bound left."""
    _stay(db, "LATE-1", entry_time=EXIT + timedelta(hours=1))

    assert pss.open_stays(db, EXIT) == []


def test_clock_skew_between_the_two_cameras_is_tolerated(db):
    """Entry time comes from the entry camera's clock and exit time from the
    exit camera's. Two Hikvision devices drift by seconds, so a car that leaves
    within the drift of arriving must still find its own stay."""
    _stay(db, "FAST-1", entry_time=EXIT + timedelta(seconds=30))

    assert [row.plate_number for row in pss.open_stays(db, EXIT)] == ["FAST-1"]

    # ...but the tolerance is small, not a licence to match tomorrow's car.
    assert pss.open_stays(db, EXIT - timedelta(hours=2)) == []


# ── ambiguity is surfaced, not guessed ──────────────────────────────────────


def test_two_open_stays_on_one_plate_name_the_stranded_one(db, caplog):
    """Two open stays under one plate means an exit was MISSED: the car came back
    and its previous stay was never closed. The newest is what this exit ends;
    the older is a stranded stay that needs its own answer, so it is named in the
    log rather than silently skipped by a `.first()`."""
    older = _stay(db, "TWO-1", entry_time=ENTRY - timedelta(hours=5))
    newer = _stay(db, "TWO-1", entry_time=ENTRY)

    with caplog.at_level("WARNING"):
        closed = pss.close_session(
            db, plate_number="TWO-1", event_time=EXIT,
            camera_id="CAM-EXIT", snapshot_path=None,
        )

    assert closed.id == newer.id
    db.refresh(older)
    assert older.status == "open", "the stranded stay must not be silently closed"
    assert any(
        "STRANDED" in r.message and f"id={older.id}" in r.message
        for r in caplog.records
    )


# ── the close is guarded ────────────────────────────────────────────────────


def test_a_second_writer_cannot_close_the_same_stay_twice(db):
    """The edge webhook and the reconcile sweep both run the full pipeline since
    Stage 1, so two writers can reach one stay. A stale read would move its
    exit_time and duration to whichever finished last.

    The guard is the UPDATE's own `WHERE status = 'open'`, so the database
    decides — not a read this transaction did earlier.
    """
    stay = _stay(db, "RACE-1")

    first = pss.close_session(
        db, plate_number="RACE-1", event_time=EXIT,
        camera_id="CAM-EXIT", snapshot_path=None,
    )
    assert first is not None
    first_exit_time = first.exit_time

    # A second writer holding the same stale row object.
    second = pss.close_matched_session(
        db, stay, exit_time=EXIT + timedelta(hours=1),
        camera_id="CAM-EXIT", snapshot_path=None,
    )

    assert second is None, "the second close must refuse"
    db.refresh(stay)
    assert stay.exit_time == first_exit_time, "the first close must stand"


def test_a_delayed_real_exit_still_corrects_a_reentry_bound(db):
    """`close_session:210-236` handles a real exit arriving after a validated
    re-entry already closed the previous stay at an INFERRED upper bound. The
    refactor guards the close on `status='open'`, and this stay is deliberately
    `closed` — so it needs its own expected state, or the correction is refused.
    """
    stay = _stay(db, "LATE-2", entry_time=ENTRY)
    stay.status = "closed"
    stay.exit_camera_id = pss.REENTRY_RECONCILIATION_CAMERA_ID
    stay.exit_time = EXIT + timedelta(hours=1)
    db.flush()

    closed = pss.close_session(
        db, plate_number="LATE-2", event_time=EXIT,
        camera_id="CAM-EXIT", snapshot_path=None,
    )

    assert closed is not None and closed.id == stay.id
    assert closed.exit_time == EXIT, "the inferred bound must be replaced"
    assert closed.exit_camera_id == "CAM-EXIT"


# ── the audit trail agrees with the session ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_exit_row_is_paired_from_the_resolved_session(db, monkeypatch):
    """The stay is open as `ABC-123`; the exit reads `ABC-1234` and VA confirms.

    UC2 pairs by searching for an entry row under the EXIT's plate — the one
    string that does not match when the entry was misread. Every non-exact close
    left `matched_entry_id` NULL, so the trail could not say which entry a given
    exit ended.
    """
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
    assert exit_row.parking_duration == 9000


# ── both ingest paths agree ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hik_sourced_and_edge_sourced_exits_resolve_identically(db, monkeypatch):
    """Same car, same second, two ways of noticing it. The only thing that may
    differ is which camera the audit row names."""
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT)
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    edge_stay = _stay(db, "SDD-6707")

    await entry_exit_service.handle_anpr_event(_exit_event("6707-SDD"), db)
    db.commit()
    db.refresh(edge_stay)

    # Same situation, resolved by the sweep instead.
    hik_stay = _stay(db, "SDD-6708")

    async def fake_list(resource_ids, begin, end, db):
        return [VehicleLogRecord.from_openapi_record({
            "crossRecordSyscode": "G-EQ", "cameraIndexCode": "510",
            "plateNo": "6708SDD", "crossTime": EXIT.replace(tzinfo=FTZ).isoformat(),
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
    db.refresh(hik_stay)

    assert edge_stay.status == hik_stay.status == "closed"
    assert edge_stay.duration_seconds == hik_stay.duration_seconds == 9000
    assert edge_stay.exit_camera_id == "CAM-EXIT"
    assert hik_stay.exit_camera_id == exit_pipeline.RECONCILE_CAMERA_ID
