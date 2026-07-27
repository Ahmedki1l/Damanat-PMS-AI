"""Matching rules and mode policy.

HikCentral validates a plate the ANPR camera already reported. The invariant
under test throughout: whatever happens, the caller gets back a usable plate and
`off`/`shadow` never change what the pipeline would have done on its own.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.hik_validation import HikValidation
from app.services.hikcentral import validation
from app.services.hikcentral.models import (
    PLATE_SOURCE_EDGE_ANPR,
    PLATE_SOURCE_HIK_CONFIRMED,
    PLATE_SOURCE_HIK_CORRECTED,
    HikImages,
    VehicleLogRecord,
)

FACILITY_TZ = timezone(timedelta(hours=3))
ANPR_PLATE = "JKA-5625"
EVENT_TIME = datetime(2026, 7, 27, 15, 5, 24)  # naive facility-local

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
def hik_authoritative(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "447")


def _record(plate="5625JKA", guid="GUID-1", offset_seconds=0):
    return VehicleLogRecord.from_payload(
        {
            "GUID": guid,
            "PassTime": (
                datetime(2026, 7, 27, 15, 5, 24, tzinfo=FACILITY_TZ)
                + timedelta(seconds=offset_seconds)
            ).isoformat(),
            "PlateLicense": plate,
            "VehicleImageUrl": "Vsm://v",
            "PlateImageUrl": "Vsm://p",
            "ResourceID": "447",
        }
    )


def _stub_lookup(monkeypatch, records, calls=None):
    async def fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        return list(records)

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", fake
    )


# ── Mode: off ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_mode_never_contacts_hikcentral(monkeypatch):
    async def explode(**kwargs):  # pragma: no cover
        raise AssertionError("HikCentral must not be queried when mode=off")

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", explode
    )
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_EDGE_ANPR
    assert outcome.matched is False
    assert outcome.record is None


@pytest.mark.asyncio
async def test_missing_resource_ids_degrades_to_the_anpr_plate(monkeypatch):
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "")
    _stub_lookup(monkeypatch, [_record()])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == ANPR_PLATE
    assert outcome.reason == "no_resource_ids_configured"


# ── Window construction ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lookup_window_is_anchored_on_the_gate_event(monkeypatch):
    calls = []
    _stub_lookup(monkeypatch, [], calls)
    monkeypatch.setattr(settings, "HIK_QUERY_LOOKBACK_SECONDS", 30.0)
    monkeypatch.setattr(settings, "HIK_QUERY_LOOKAHEAD_SECONDS", 5.0)
    monkeypatch.setattr(settings, "HIK_QUERY_PAGE_SIZE", 5)

    await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert len(calls) == 1
    call = calls[0]
    # Naive facility-local in, tz-aware out — HikCentral must never be asked in
    # an ambiguous timezone.
    assert call["begin"].tzinfo is not None
    assert call["begin"] == datetime(
        2026, 7, 27, 15, 4, 54, tzinfo=FACILITY_TZ
    )
    assert call["end"] == datetime(2026, 7, 27, 15, 5, 29, tzinfo=FACILITY_TZ)
    assert call["resource_ids"] == "447"
    assert call["page_size"] == 5
# ── Case A: agreement ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_matching_plate_is_confirmed(monkeypatch):
    _stub_lookup(monkeypatch, [_record()])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.matched is True
    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_HIK_CONFIRMED
    assert outcome.guid == "GUID-1"


@pytest.mark.asyncio
async def test_closest_passtime_wins_among_agreeing_records(monkeypatch):
    _stub_lookup(
        monkeypatch,
        [
            _record(guid="FAR", offset_seconds=-8),
            _record(guid="NEAR", offset_seconds=-1),
        ],
    )

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.guid == "NEAR"


@pytest.mark.asyncio
async def test_record_outside_the_skew_window_is_not_a_match(monkeypatch):
    monkeypatch.setattr(settings, "HIK_MATCH_MAX_SKEW_SECONDS", 10.0)
    _stub_lookup(monkeypatch, [_record(offset_seconds=-25)])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    # It fell inside the query window but is too far from the gate event to be
    # the same car.
    assert outcome.matched is False
    assert outcome.plate == ANPR_PLATE
    assert outcome.reason == "no_matching_record"


@pytest.mark.asyncio
async def test_no_records_keeps_the_anpr_plate(monkeypatch):
    _stub_lookup(monkeypatch, [])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_EDGE_ANPR
    assert outcome.reason == "no_hik_record"


# ── Case A: disagreement ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_authoritative_mode_replaces_a_disagreeing_plate(monkeypatch):
    _stub_lookup(monkeypatch, [_record(plate="9990BHD", guid="OTHER")])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.matched is True
    assert outcome.plate == "BHD-9990"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CORRECTED
    assert outcome.reported_plate == ANPR_PLATE


@pytest.mark.asyncio
async def test_shadow_mode_keeps_the_anpr_plate_but_records_the_evidence(
    monkeypatch,
):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "shadow")
    _stub_lookup(monkeypatch, [_record(plate="9990BHD", guid="OTHER")])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    # Shadow must change nothing the pipeline does...
    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_EDGE_ANPR
    assert outcome.matched is False
    assert outcome.reason == "plate_mismatch_shadow"
    # ...but the disagreement is still captured, which is the point of shadow.
    assert outcome.record is not None
    assert outcome.record.canonical_plate == "BHD-9990"


@pytest.mark.asyncio
async def test_unreadable_hik_plate_is_not_treated_as_a_correction(monkeypatch):
    _stub_lookup(monkeypatch, [_record(plate="", guid="BLANK")])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_EDGE_ANPR


# ── GUID identity ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_already_consumed_guid_cannot_back_a_second_event(monkeypatch, db):
    db.add(
        HikValidation(
            direction="entry",
            guid="GUID-1",
            plate_source=PLATE_SOURCE_HIK_CONFIRMED,
            matched=True,
            created_at=datetime.now(),
        )
    )
    db.flush()
    _stub_lookup(monkeypatch, [_record(guid="GUID-1")])

    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME, db)

    # One HikCentral vehicle pass may justify exactly one gate event.
    assert outcome.matched is False
    assert outcome.plate == ANPR_PLATE


# ── Persistence ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_hik_validation_persists_the_decision(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record()])
    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME, db)

    row = validation.record_hik_validation(
        db,
        outcome=outcome,
        direction=validation.DIRECTION_ENTRY,
        images=HikImages(vehicle_image_path="/snap/v.jpg"),
    )
    db.flush()

    assert row is not None
    assert row.guid == "GUID-1"
    assert row.plate_license == "5625JKA"
    assert row.canonical_plate == "JKA-5625"
    assert row.reported_plate == ANPR_PLATE
    assert row.vehicle_image_path == "/snap/v.jpg"
    # Stored naive facility-local, matching every other writer in the codebase.
    assert row.pass_time == EVENT_TIME
    assert row.pass_time.tzinfo is None


@pytest.mark.asyncio
async def test_record_hik_validation_refuses_a_duplicate_guid(monkeypatch, db):
    _stub_lookup(monkeypatch, [_record()])
    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME, db)

    assert (
        validation.record_hik_validation(
            db, outcome=outcome, direction=validation.DIRECTION_ENTRY
        )
        is not None
    )
    db.flush()
    assert (
        validation.record_hik_validation(
            db, outcome=outcome, direction=validation.DIRECTION_ENTRY
        )
        is None
    )


def test_record_hik_validation_ignores_an_outcome_without_evidence(db):
    assert (
        validation.record_hik_validation(
            db, outcome=None, direction=validation.DIRECTION_ENTRY
        )
        is None
    )


# ── Image download ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_images_are_not_downloaded_without_a_matched_record(monkeypatch):
    async def explode(url):  # pragma: no cover
        raise AssertionError("no download without a HikCentral record")

    monkeypatch.setattr(
        "app.services.hikcentral.client.download_picture", explode
    )

    assert not await validation.download_hik_images(None)


@pytest.mark.asyncio
async def test_images_are_downloaded_and_published_as_snapshot_urls(
    monkeypatch, tmp_path
):
    _stub_lookup(monkeypatch, [_record()])
    outcome = await validation.validate_entry_plate(ANPR_PLATE, EVENT_TIME)

    async def fake_download(url):
        return b"jpeg:" + url.encode()

    monkeypatch.setattr(
        "app.services.hikcentral.client.download_picture", fake_download
    )
    monkeypatch.setattr(
        "app.services.hikcentral.client.SNAPSHOT_DIR", str(tmp_path)
    )

    images = await validation.download_hik_images(outcome)

    assert images.vehicle_image_path.endswith(".jpg")
    assert "/pms-ai/snapshots/" in images.vehicle_image_path
    assert "/pms-ai/snapshots/" in images.plate_image_path
    assert images.vehicle_local_path != images.plate_local_path
