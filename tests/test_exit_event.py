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
from app.services import entry_exit_service, exit_pipeline, hikcentral
from app.services.event_parser import ParsedCameraEvent
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
