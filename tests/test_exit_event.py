"""One exit pipeline, whichever path noticed the car.

The failure these guard against is structural rather than numeric: the edge
webhook and the HikCentral reconcile sweep resolved exits differently. The sweep
called `close_session` and stopped, so a recovered exit for a car whose ENTRY
plate was misread matched nothing, closed nothing, and consumed its GUID on the
way out — `SNA-226` sat on the dashboard as a 75h overstay for a car that had
driven home.
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
    parking_session_service,
)
from app.services.event_parser import ParsedCameraEvent
from app.services.hikcentral import validation
from app.services.hikcentral.models import VehicleLogRecord

FTZ = timezone(timedelta(hours=3))
EXIT_TIME = datetime(2026, 8, 16, 9, 0, 0)  # naive facility-local

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
def exit_layer(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "510")
    monkeypatch.setattr(
        settings,
        "CAMERAS",
        {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}},
    )
    monkeypatch.setattr(
        "app.services.hikcentral.client.SNAPSHOT_DIR", str(tmp_path)
    )
    monkeypatch.setattr(
        "app.utils.core_backend_client.notify_pms_anpr", AsyncMock()
    )


def _exit_event(plate="JKA-5625", at=EXIT_TIME):
    return ParsedCameraEvent(
        camera_id="CAM-EXIT",
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id="exit",
        channel_name="Exit ANPR",
        trigger_time=at.replace(tzinfo=FTZ),
        raw_xml="{}",
        plate_number=plate,
        gate="exit",
        snapshot_path="/snap/exit.jpg",
    )


def _hik_record(guid, plate, at=EXIT_TIME):
    return VehicleLogRecord.from_openapi_record({
        "crossRecordSyscode": guid,
        "cameraIndexCode": "510",
        "plateNo": plate,
        "crossTime": at.replace(tzinfo=FTZ).isoformat(),
        "vehiclePicUri": "Vsm://v",
    })


def _open_session(db, plate, entry_time=datetime(2026, 8, 16, 7, 0, 0)):
    row = ParkingSession(
        plate_number=plate,
        entry_time=entry_time,
        entry_camera_id="CAM-ENTRY",
        status="open",
        created_at=entry_time,
        updated_at=entry_time,
    )
    db.add(row)
    db.flush()
    return row


def _hik_returns(monkeypatch, *records):
    async def _query(**kwargs):
        return list(records)

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", _query
    )


# ── both ingest paths build the same event ──────────────────────────────────


@pytest.mark.asyncio
async def test_both_sources_describe_the_same_car_identically(monkeypatch, db):
    """Same car, same second — the only fields that may differ are the ones that
    record WHERE it was noticed."""
    _hik_returns(monkeypatch, _hik_record("G-1", "5625JKA"))

    edge = await exit_pipeline.from_camera_event(
        _exit_event(), "JKA-5625", EXIT_TIME, db
    )
    polled = exit_pipeline.from_polled_outcome(
        hikcentral.polled_outcome(_hik_record("G-1", "5625JKA")),
        "/snap/exit.jpg",
    )

    assert edge.plate == polled.plate == "JKA-5625"
    assert edge.event_time == polled.event_time == EXIT_TIME
    assert edge.snapshot_path == polled.snapshot_path
    assert edge.source == exit_pipeline.SOURCE_EDGE
    assert polled.source == exit_pipeline.SOURCE_HIK_RECONCILE
    assert polled.from_reconcile and not edge.from_reconcile


@pytest.mark.asyncio
async def test_hikcentral_correction_is_carried_by_the_event(monkeypatch, db):
    """The platform disagrees within the skew window, so its plate is the one
    every downstream step sees."""
    _hik_returns(monkeypatch, _hik_record("G-2", "5625JKB"))

    built = await exit_pipeline.from_camera_event(
        _exit_event(), "JKA-5625", EXIT_TIME, db
    )

    assert built.plate == "JKB-5625"
    assert built.hik_guid == "G-2"


@pytest.mark.asyncio
async def test_an_unreachable_platform_degrades_to_the_edge_plate(monkeypatch, db):
    """Never raises. The exit camera is the reliable read; HikCentral is the
    second opinion, and a second opinion that does not arrive changes nothing.

    Patched at the transport, not at `query_vehicle_logs` — the "returns [] on
    any failure" contract lives inside that function, so stubbing it out would
    remove the guard this is meant to prove.
    """

    async def unreachable(path, body_obj):
        return None

    monkeypatch.setattr(
        "app.services.hikcentral.client._signed_post", unreachable
    )

    built = await exit_pipeline.from_camera_event(
        _exit_event(), "JKA-5625", EXIT_TIME, db
    )

    assert built.plate == "JKA-5625"
    assert built.hik_guid is None


@pytest.mark.asyncio
async def test_no_exit_resource_id_is_a_silent_no_op(monkeypatch, db):
    """The `453` failure mode: an unset/unknown indexCode must leave the edge
    plate standing rather than blocking the exit."""
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "")

    async def explode(**kwargs):  # pragma: no cover
        raise AssertionError("must not query without a configured camera")

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", explode
    )

    built = await exit_pipeline.from_camera_event(
        _exit_event(), "JKA-5625", EXIT_TIME, db
    )

    assert built.plate == "JKA-5625"


# ── dedup moves to the corrected plate ──────────────────────────────────────


@pytest.mark.asyncio
async def test_two_misreads_of_one_car_dedup_after_correction(monkeypatch, db):
    """`AAA-2538` exited on 8/11 and again on 8/12 under two different misreads.

    The ±30s dedup keys on an exact plate, so two spellings of one car cleared it
    and both were processed. Corrected first, the second read is a duplicate.
    """
    _open_session(db, "KXR-2538")
    # Both edge reads are wrong in different ways; HikCentral holds the truth.
    _hik_returns(monkeypatch, _hik_record("G-3", "2538KXR"))

    first = await entry_exit_service.handle_anpr_event(
        _exit_event("AAA-2538"), db
    )
    second = await entry_exit_service.handle_anpr_event(
        _exit_event("AAB-2538", at=EXIT_TIME + timedelta(seconds=10)), db
    )
    db.commit()

    rows = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").all()
    assert len(rows) == 1, "one car leaving must produce one exit row"
    assert rows[0].plate_number == "KXR-2538"
    # The duplicate still returns its forward so VA is not left un-notified.
    assert first is not None and second is not None
    assert second.plate == "KXR-2538"


# ── the reconcile sweep reaches the matcher ─────────────────────────────────


@pytest.mark.asyncio
async def test_a_recovered_exit_reaches_the_matcher(monkeypatch, db):
    """SNA-226. The stay is open under a misread ENTRY plate, so the exact plate
    closes nothing — before this the sweep stopped there and consumed the GUID.

    The misread is in the letters, which is what the entry LPR actually gets
    wrong: the digit group is intact, so `exit_match_service` resolves it
    deterministically with no appearance evidence needed.
    """
    stay = _open_session(db, "SNP-226")   # entry read the letters wrong
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT_TIME)

    async def fake_list(resource_ids, begin, end, db):
        return [_hik_record("G-4", "226SNA", EXIT_TIME - timedelta(hours=1))]

    async def fake_images(outcome):
        from app.services.hikcentral.models import HikImages
        return HikImages()

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)
    monkeypatch.setattr(hikcentral, "download_hik_images", fake_images)

    await entry_exit_service._reconcile_missed_exits(
        db, window=(EXIT_TIME - timedelta(hours=3), EXIT_TIME)
    )

    db.refresh(stay)
    assert stay.status == "closed", (
        "a recovered exit must reach exit_match_service, not stop at the plate"
    )


@pytest.mark.asyncio
async def test_a_recovered_exit_closes_nothing_when_the_matcher_declines(
    monkeypatch, db
):
    """The matcher's silence is an answer. An inconclusive recovery must leave
    every open stay alone rather than close the nearest one."""
    stay = _open_session(db, "ZZZ-1111")
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT_TIME)

    async def fake_list(resource_ids, begin, end, db):
        return [_hik_record("G-5", "226SNA", EXIT_TIME - timedelta(hours=1))]

    async def fake_images(outcome):
        from app.services.hikcentral.models import HikImages
        return HikImages()

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)
    monkeypatch.setattr(hikcentral, "download_hik_images", fake_images)

    await entry_exit_service._reconcile_missed_exits(
        db, window=(EXIT_TIME - timedelta(hours=3), EXIT_TIME)
    )

    db.refresh(stay)
    assert stay.status == "open"
    assert db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").count() == 1, (
        "the exit still gets its audit row even when no stay could be found"
    )


# ── the close itself can refuse ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_refused_close_is_not_reported_as_resolved(monkeypatch, db, caplog):
    """The matcher naming a stay is not the same as the stay being closed.

    `close_matched_session` cannot return None today — `_close_session_record`
    returns the row unconditionally. It becomes reachable the moment the close is
    guarded (`UPDATE ... WHERE status='open'`), which is how a stale read is
    stopped from double-closing a stay another writer already ended.

    The failure that would follow is silent, which is why this is pinned before
    the guard exists: `ExitOutcome` would carry `session=None` alongside a
    matched resolution, `.closed` and `.corrected` would both read False, and the
    only trace in the log would be a line claiming the exit resolved.
    """
    _open_session(db, "SNP-226")
    monkeypatch.setattr(
        parking_session_service, "close_matched_session",
        lambda *a, **k: None,
    )

    event = exit_pipeline.ExitEvent(
        plate="SNA-226",
        event_time=EXIT_TIME,
        camera_id="CAM-EXIT",
        snapshot_path="/snap/exit.jpg",
        source=exit_pipeline.SOURCE_EDGE,
    )
    with caplog.at_level("WARNING"):
        outcome = await exit_pipeline.resolve(db, event)

    assert outcome.match is not None and outcome.match.matched, (
        "the matcher did find the stay — only the close refused"
    )
    assert not outcome.closed
    assert not outcome.corrected, (
        "corrected gates the caller's duration backfill; a stay that was never "
        "closed has no entry_time to read"
    )
    assert not any("resolved to session" in r.message for r in caplog.records), (
        "a refused close must never be logged as a resolved exit"
    )
    assert any("close was refused" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_a_refused_close_leaves_the_stay_open_and_writes_no_duration(
    monkeypatch, db
):
    """End to end through the edge path: the audit row is still written, the stay
    is untouched, and no duration is invented from a session that never closed."""
    stay = _open_session(db, "SNP-226")
    _hik_returns(monkeypatch, _hik_record("G-6", "226SNA"))
    monkeypatch.setattr(
        parking_session_service, "close_matched_session",
        lambda *a, **k: None,
    )

    await entry_exit_service.handle_anpr_event(_exit_event("SNA-226"), db)
    db.commit()

    db.refresh(stay)
    assert stay.status == "open"
    row = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    assert row.plate_number == "SNA-226"
    assert row.parking_duration is None


# ── a plate that reached HikCentral after we asked ──────────────────────────


@pytest.mark.asyncio
async def test_the_sweep_corrects_the_edge_row_instead_of_duplicating_it(
    monkeypatch, db
):
    """One car leaving must never produce two exit rows.

    The exit fires at T and HikCentral has not ingested the pass yet, so the
    misread stands. Minutes later the sweep sees the pass, `same_vehicle_plate`
    cannot tie `KXR-2538` to `AAA-2538`, and before this it wrote a SECOND exit
    row for the same car at the same second — a phantom exit in every count.
    """
    _open_session(db, "KXR-2538")
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 60.0)
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0)  # sweep only
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT_TIME)

    # 1. The car leaves. The platform has nothing yet.
    _hik_returns(monkeypatch)
    await entry_exit_service.handle_anpr_event(_exit_event("AAA-2538"), db)
    db.commit()
    assert db.query(ParkingSession).one().status == "closed", (
        "the digit-group rule already closes this one on the misread"
    )

    # 2. Minutes later the sweep finds the pass the platform has now ingested.
    async def fake_list(resource_ids, begin, end, db):
        return [_hik_record("LATE-1", "2538KXR", EXIT_TIME)]

    async def fake_images(outcome):
        from app.services.hikcentral.models import HikImages
        return HikImages()

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)
    monkeypatch.setattr(hikcentral, "download_hik_images", fake_images)
    monkeypatch.setattr(
        entry_exit_service, "facility_now_naive",
        lambda: EXIT_TIME + timedelta(minutes=5),
    )
    await entry_exit_service._reconcile_missed_exits(
        db, window=(EXIT_TIME - timedelta(hours=1), EXIT_TIME + timedelta(minutes=5))
    )
    db.commit()

    rows = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").all()
    assert len(rows) == 1, "the late plate must correct the row, not add one"
    assert rows[0].plate_number == "KXR-2538"


@pytest.mark.asyncio
async def test_two_exits_in_the_window_are_too_ambiguous_to_adopt(monkeypatch, db):
    """Time alone is only evidence when it names ONE car.

    Two cars queued at the gate inside the match window: nothing says which of
    them the late pass belongs to, so it falls back to being treated as a missed
    exit. A duplicate row is cheap; attaching a stranger's plate to an exit is not.
    """
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 60.0)
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0)
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT_TIME)

    _hik_returns(monkeypatch)
    await entry_exit_service.handle_anpr_event(_exit_event("AAA-2538"), db)
    await entry_exit_service.handle_anpr_event(
        _exit_event("BBB-7777", at=EXIT_TIME + timedelta(seconds=20)), db
    )
    db.commit()

    assert exit_pipeline.exit_row_for_late_pass(
        db, EXIT_TIME, timedelta(seconds=60)
    ) is None


@pytest.mark.asyncio
async def test_a_row_already_backed_by_a_pass_is_not_adopted_twice(monkeypatch, db):
    """One HikCentral pass, one exit row. A second pass in the same window must
    not overwrite a row the first one already claimed."""
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0)
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT_TIME)
    _hik_returns(monkeypatch)
    await entry_exit_service.handle_anpr_event(_exit_event("AAA-2538"), db)
    db.commit()

    row = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    hikcentral.record_hik_validation(
        db,
        outcome=hikcentral.polled_outcome(_hik_record("CLAIM-1", "2538KXR")),
        direction=hikcentral.DIRECTION_EXIT,
        entry_exit_log_id=row.id,
    )
    db.commit()

    assert exit_pipeline.exit_row_for_late_pass(
        db, EXIT_TIME, timedelta(seconds=60)
    ) is None


@pytest.mark.asyncio
async def test_the_deferred_recheck_corrects_the_row_and_consumes_the_guid(
    monkeypatch, db
):
    """The second ask, 15s later — here with the delay collapsed to zero.

    Measured: every successful lookup in ai-logs.txt landed 7-44s after its pass
    because the entry path waits for a crossing first. The exit path asks at
    ~2-3s, so an empty answer is "not yet", not "never".
    """
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT_TIME)
    _open_session(db, "KXR-2538")

    # The exit path's own lookup finds nothing; the recheck finds the pass.
    answers = [[], [_hik_record("LATE-2", "2538KXR")]]

    async def _query(**kwargs):
        return answers.pop(0) if answers else []

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", _query
    )
    # The detached task opens its own session; point it at this one.
    monkeypatch.setattr(
        "app.database.SessionLocal", lambda: _NonClosing(db)
    )

    await entry_exit_service.handle_anpr_event(_exit_event("AAA-2538"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    row = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    assert row.plate_number == "KXR-2538", "the late plate must reach the audit row"
    assert validation.guid_already_used(db, "LATE-2"), (
        "the recheck must consume the pass, or the sweep redoes it as a duplicate"
    )


@pytest.mark.asyncio
async def test_no_recheck_is_scheduled_when_the_platform_already_answered(
    monkeypatch, db
):
    """A pass HikCentral HAS and disagrees about is a settled answer — asking the
    same question again is load with no possible new information."""
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 30)
    _hik_returns(monkeypatch, _hik_record("G-7", "5625JKB"))

    built = await exit_pipeline.from_camera_event(
        _exit_event(), "JKA-5625", EXIT_TIME, db
    )

    assert built.hik_reason == "plate_corrected"
    assert not built.plate_may_still_arrive
    exit_pipeline.schedule_late_plate_recheck(built, 1)
    assert not exit_pipeline._late_rechecks


class _NonClosing:
    """A SessionLocal stand-in that hands out the test's session and ignores
    `close()`, so a detached task can be observed after it finishes."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        if name == "close":
            return lambda: None
        return getattr(self._session, name)
