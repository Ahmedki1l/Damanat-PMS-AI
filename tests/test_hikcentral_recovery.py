"""Case B: a ramp crossing the ANPR camera never labelled.

Today such a crossing raises `silent_entry` and creates nothing — the car is
lost. These tests cover the recovery path end to end, and just as importantly
that it declines whenever the evidence is ambiguous.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.alert import Alert
from app.models.entry_exit_log import EntryExitLog
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.services import entry_exit_service
from app.services.hikcentral import validation
from app.services.hikcentral.models import (
    PLATE_SOURCE_HIK_RECOVERED,
    VehicleLogRecord,
)

FACILITY_TZ = timezone(timedelta(hours=3))
CROSSING_TIME = datetime(2026, 7, 27, 15, 5, 40)  # naive facility-local

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
def hik_authoritative(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "447")
    monkeypatch.setattr(
        "app.services.hikcentral.client.SNAPSHOT_DIR", str(tmp_path)
    )
    # No network: the VA forward is the only outbound call in the flush path.
    monkeypatch.setattr(
        "app.utils.core_backend_client.notify_pms_anpr", AsyncMock()
    )
    entry_exit_service._entry_bursts.clear()
    entry_exit_service._pending_crossings.clear()
    entry_exit_service._recent_entries.clear()
    yield
    entry_exit_service._entry_bursts.clear()
    entry_exit_service._pending_crossings.clear()
    entry_exit_service._recent_entries.clear()


def _record(plate="5625JKA", guid="GUID-1", offset_seconds=-15):
    """A pass at the entry LPR, which happens BEFORE the ramp crossing."""
    return VehicleLogRecord.from_payload(
        {
            "GUID": guid,
            "PassTime": (
                datetime(2026, 7, 27, 15, 5, 40, tzinfo=FACILITY_TZ)
                + timedelta(seconds=offset_seconds)
            ).isoformat(),
            "PlateLicense": plate,
            "VehicleImageUrl": "Vsm://v",
            "PlateImageUrl": "Vsm://p",
            "ResourceID": "447",
        }
    )


def _stub_lookup(monkeypatch, records):
    async def fake(**kwargs):
        return list(records)

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", fake
    )


def _stub_images(monkeypatch):
    async def fake_download(url):
        return b"jpeg"

    monkeypatch.setattr(
        "app.services.hikcentral.client.download_picture", fake_download
    )


def _crossing(source="CAM-23", snapshot="/snap/cam23.jpg"):
    return {"ts": CROSSING_TIME, "snapshot": snapshot, "source": source}


# ── recover_entry_plate: the uniqueness rule ────────────────────────────────


@pytest.mark.asyncio
async def test_single_candidate_is_recovered(monkeypatch):
    _stub_lookup(monkeypatch, [_record()])

    outcome = await validation.recover_entry_plate(CROSSING_TIME, "CAM-23")

    assert outcome is not None
    assert outcome.plate == "JKA-5625"
    assert outcome.plate_source == PLATE_SOURCE_HIK_RECOVERED
    assert outcome.reported_plate is None


@pytest.mark.asyncio
async def test_no_candidate_declines(monkeypatch):
    _stub_lookup(monkeypatch, [])
    assert await validation.recover_entry_plate(CROSSING_TIME, "CAM-23") is None


@pytest.mark.asyncio
async def test_two_candidates_decline(monkeypatch):
    _stub_lookup(
        monkeypatch,
        [_record(guid="A"), _record(plate="9990BHD", guid="B")],
    )

    # With two cars in the window nothing says which one crossed; guessing
    # would staple a stranger's plate onto the session.
    assert await validation.recover_entry_plate(CROSSING_TIME, "CAM-23") is None


@pytest.mark.asyncio
async def test_plateless_record_is_not_a_candidate(monkeypatch):
    _stub_lookup(monkeypatch, [_record(plate="")])
    assert await validation.recover_entry_plate(CROSSING_TIME, "CAM-23") is None


@pytest.mark.asyncio
async def test_already_consumed_guid_is_not_a_candidate(monkeypatch, db):
    db.add(
        HikValidation(
            direction="entry",
            guid="GUID-1",
            plate_source=PLATE_SOURCE_HIK_RECOVERED,
            matched=True,
            created_at=datetime.now(),
        )
    )
    db.flush()
    _stub_lookup(monkeypatch, [_record(guid="GUID-1")])

    assert (
        await validation.recover_entry_plate(CROSSING_TIME, "CAM-23", db) is None
    )


@pytest.mark.asyncio
async def test_shadow_mode_does_not_recover(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "shadow")
    _stub_lookup(monkeypatch, [_record()])

    # Shadow measures the recovery rate; it must not create sessions that do
    # not exist today.
    assert await validation.recover_entry_plate(CROSSING_TIME, "CAM-23") is None


@pytest.mark.asyncio
async def test_disabled_mode_does_not_query(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")

    async def explode(**kwargs):  # pragma: no cover
        raise AssertionError("HikCentral must not be queried when mode=off")

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", explode
    )

    assert await validation.recover_entry_plate(CROSSING_TIME, "CAM-23") is None


# ── The synthesized burst ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_recovered_burst_looks_like_a_confirmed_anpr_burst(monkeypatch):
    _stub_lookup(monkeypatch, [_record()])
    outcome = await validation.recover_entry_plate(CROSSING_TIME, "CAM-23")

    buf = entry_exit_service._recovered_burst(outcome, _crossing())

    assert buf["confirmed"] is True
    assert buf["force_flush"] is True
    assert buf["camera_id"] == "CAM-ENTRY"
    assert len(buf["reads"]) == 1
    assert buf["reads"][0]["plate"] == "JKA-5625"
    # The entry is timed by HikCentral's PassTime, not by when the crossing
    # expired, and stored naive facility-local.
    assert buf["reads"][0]["event_time"] == datetime(2026, 7, 27, 15, 5, 25)
    assert buf["reads"][0]["event_time"].tzinfo is None
    # The CAM-23 image still reaches VA as a confirmation snapshot.
    assert buf["confirm_snapshots"] == {"CAM-23": "/snap/cam23.jpg"}
    # Carrying the record forward is what keeps this to ONE lookup per car.
    assert buf["hik_outcome"] is outcome


# ── End to end through the real flush path ──────────────────────────────────


@pytest.mark.asyncio
async def test_recovery_creates_a_real_entry_and_session(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record()])
    _stub_images(monkeypatch)

    assert await entry_exit_service._recover_silent_entry(db, _crossing()) is True

    log = db.query(EntryExitLog).one()
    assert log.plate_number == "JKA-5625"
    assert log.gate == "entry"
    assert log.camera_id == "CAM-ENTRY"

    session = db.query(ParkingSession).one()
    assert session.plate_number == "JKA-5625"
    assert session.status == "open"
    # A recovered entry has no gate image of its own — HikCentral's fills in.
    assert "/pms-ai/snapshots/" in session.entry_snapshot_path

    evidence = db.query(HikValidation).one()
    assert evidence.guid == "GUID-1"
    assert evidence.plate_source == PLATE_SOURCE_HIK_RECOVERED
    assert evidence.reported_plate is None
    assert evidence.session_id == session.id
    assert evidence.entry_exit_log_id == log.id
    assert evidence.matched is True


@pytest.mark.asyncio
async def test_declined_recovery_writes_nothing(monkeypatch, db):
    _stub_lookup(monkeypatch, [])

    assert await entry_exit_service._recover_silent_entry(db, _crossing()) is False
    assert db.query(EntryExitLog).count() == 0
    assert db.query(ParkingSession).count() == 0
    assert db.query(HikValidation).count() == 0


@pytest.mark.asyncio
async def test_expired_crossing_recovers_instead_of_alerting(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record()])
    _stub_images(monkeypatch)
    entry_exit_service._pending_crossings.append(
        {
            "ts": CROSSING_TIME,
            "expires_at_monotonic": 0.0,  # already expired
            "snapshot": "/snap/cam23.jpg",
            "source": "CAM-23",
        }
    )

    await entry_exit_service.flush_due_entry_bursts(db)

    assert db.query(ParkingSession).count() == 1
    # The whole point: this car is no longer a silent entry.
    assert (
        db.query(Alert).filter(Alert.alert_type == "silent_entry").count() == 0
    )


@pytest.mark.asyncio
async def test_expired_crossing_still_alerts_when_recovery_declines(
    monkeypatch, db
):
    _stub_lookup(monkeypatch, [])
    entry_exit_service._pending_crossings.append(
        {
            "ts": CROSSING_TIME,
            "expires_at_monotonic": 0.0,
            "snapshot": "/snap/cam23.jpg",
            "source": "CAM-23",
        }
    )

    await entry_exit_service.flush_due_entry_bursts(db)

    # Previous behaviour is preserved exactly when HikCentral cannot help.
    assert (
        db.query(Alert).filter(Alert.alert_type == "silent_entry").count() == 1
    )
    assert db.query(ParkingSession).count() == 0


# ── Case A through the flush path ───────────────────────────────────────────


def _burst(plate="JKA-5625", event_time=None):
    event_time = event_time or datetime(2026, 7, 27, 15, 5, 25)
    return {
        "id": 1,
        "camera_id": "CAM-ENTRY",
        "reads": [
            {
                "plate": plate,
                "confidence": 90,
                "pic_num": 1,
                "event_time": event_time,
                "snapshot_path": "/snap/gate.jpg",
                "local_snapshot_path": "/local/gate.jpg",
            }
        ],
        "first_event_time": event_time,
        "last_read_at": event_time,
        "confirmed": True,
        "confirm_snapshots": {},
        "confirm_source": "CAM-23",
        "force_flush": True,
    }


@pytest.mark.asyncio
async def test_authoritative_correction_reaches_the_database(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record(plate="9990BHD", guid="G2", offset_seconds=-15)])
    _stub_images(monkeypatch)

    await entry_exit_service._flush_entry_burst(db, _burst())

    # Everything downstream — log, session, evidence — sees one plate.
    assert db.query(EntryExitLog).one().plate_number == "BHD-9990"
    assert db.query(ParkingSession).one().plate_number == "BHD-9990"
    evidence = db.query(HikValidation).one()
    assert evidence.canonical_plate == "BHD-9990"
    assert evidence.reported_plate == "JKA-5625"


@pytest.mark.asyncio
async def test_shadow_correction_does_not_reach_the_database(monkeypatch, db):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "shadow")
    _stub_lookup(monkeypatch, [_record(plate="9990BHD", guid="G2", offset_seconds=-15)])
    _stub_images(monkeypatch)

    await entry_exit_service._flush_entry_burst(db, _burst())

    assert db.query(EntryExitLog).one().plate_number == "JKA-5625"
    # The disagreement is still recorded, so the mismatch rate is measurable.
    assert db.query(HikValidation).one().canonical_plate == "BHD-9990"


@pytest.mark.asyncio
async def test_no_images_downloaded_for_a_duplicate_burst(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record(offset_seconds=-15)])

    async def explode(url):  # pragma: no cover
        raise AssertionError(
            "a burst suppressed as a duplicate never becomes a session, "
            "so its imagery must never be fetched"
        )

    monkeypatch.setattr(
        "app.services.hikcentral.client.download_picture", explode
    )
    # An entry for this plate seconds earlier makes the next burst a duplicate.
    db.add(
        EntryExitLog(
            plate_number="JKA-5625",
            gate="entry",
            camera_id="CAM-ENTRY",
            event_time=datetime(2026, 7, 27, 15, 5, 20),
            created_at=datetime(2026, 7, 27, 15, 5, 20),
        )
    )
    db.flush()

    await entry_exit_service._flush_entry_burst(db, _burst())

    assert db.query(ParkingSession).count() == 0


@pytest.mark.asyncio
async def test_only_one_lookup_per_burst(monkeypatch, db):
    calls = []

    async def counting(**kwargs):
        calls.append(kwargs)
        return [_record(offset_seconds=-15)]

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", counting
    )
    _stub_images(monkeypatch)

    await entry_exit_service._flush_entry_burst(db, _burst())

    assert len(calls) == 1


# ── Exit validation through handle_anpr_event ───────────────────────────────


def _exit_event(plate="JKA-5625"):
    return entry_exit_service.ParsedCameraEvent(
        camera_id="CAM-EXIT",
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id="exit",
        channel_name="Exit ANPR",
        trigger_time=datetime(2026, 7, 27, 15, 5, 40, tzinfo=FACILITY_TZ),
        raw_xml="{}",
        plate_number=plate,
        gate="exit",
        snapshot_path="/snap/exit.jpg",
    )


@pytest.fixture
def exit_cameras(monkeypatch):
    monkeypatch.setattr(
        settings,
        "CAMERAS",
        {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}},
    )


@pytest.mark.asyncio
async def test_exit_consults_hikcentral_and_keeps_the_edge_plate_with_no_record(
    monkeypatch, db, exit_cameras
):
    """The exit plate IS checked now — it used to be entry-only.

    Reversed deliberately: the exit read is the only evidence that can correct a
    wrong ENTRY plate, so it is worth confirming before a session is rewritten
    from it. What must not change is the degraded path — a platform holding no
    record for this pass leaves the edge plate standing and writes no validation
    row, exactly as when the layer was never consulted at all.
    """
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "510")

    queried = []

    async def no_records(**kwargs):
        queried.append(kwargs)
        return []

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", no_records
    )
    db.add(
        ParkingSession(
            plate_number="JKA-5625",
            entry_time=datetime(2026, 7, 27, 14, 0, 0),
            entry_camera_id="CAM-ENTRY",
            status="open",
            created_at=datetime(2026, 7, 27, 14, 0, 0),
            updated_at=datetime(2026, 7, 27, 14, 0, 0),
        )
    )
    db.flush()

    await entry_exit_service.handle_anpr_event(_exit_event(), db)
    db.commit()

    assert queried, "the exit plate must be checked against HikCentral"
    assert db.query(ParkingSession).one().status == "closed"
    assert db.query(EntryExitLog).one().plate_number == "JKA-5625"
    assert db.query(HikValidation).count() == 0


@pytest.mark.asyncio
async def test_exit_is_unaffected_when_hik_is_disabled(
    monkeypatch, db, exit_cameras
):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")

    async def explode(**kwargs):  # pragma: no cover
        raise AssertionError("HikCentral must not be queried when mode=off")

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", explode
    )
    db.add(
        ParkingSession(
            plate_number="JKA-5625",
            entry_time=datetime(2026, 7, 27, 14, 0, 0),
            entry_camera_id="CAM-ENTRY",
            status="open",
            created_at=datetime(2026, 7, 27, 14, 0, 0),
            updated_at=datetime(2026, 7, 27, 14, 0, 0),
        )
    )
    db.flush()

    await entry_exit_service.handle_anpr_event(_exit_event(), db)
    db.commit()  # the router owns the transaction in production

    assert db.query(ParkingSession).one().status == "closed"
    assert db.query(HikValidation).count() == 0


@pytest.mark.asyncio
async def test_recovered_burst_is_not_looked_up_again(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record()])
    _stub_images(monkeypatch)
    outcome = await validation.recover_entry_plate(CROSSING_TIME, "CAM-23")

    calls = []

    async def counting(**kwargs):  # pragma: no cover
        calls.append(kwargs)
        return []

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", counting
    )
    await entry_exit_service._flush_entry_burst(
        db, entry_exit_service._recovered_burst(outcome, _crossing())
    )

    # The record was already fetched during recovery.
    assert calls == []
    assert db.query(ParkingSession).one().plate_number == "JKA-5625"
