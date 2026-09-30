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


# ── Case A2: the camera truncated the plate ─────────────────────────────────
#
# 2026-08-09: the entry LPR read 6294KKR, then re-read the same car as 4KKR and
# filed BOTH with HikCentral. Because the lookup is plate-blind, the fuller
# record sits in the same response — but agreement on the truncated plate used
# to short-circuit before anything else was examined, so KKR-4 was "confirmed".


def _partial_pair():
    """The fuller read, then the truncation of it three seconds later."""
    return [
        _record(plate="6294KKR", guid="FULL", offset_seconds=-3),
        _record(plate="4KKR", guid="PARTIAL", offset_seconds=0),
    ]


@pytest.mark.asyncio
async def test_a_fuller_record_beats_agreement_on_a_truncated_plate(monkeypatch):
    _stub_lookup(monkeypatch, _partial_pair())

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    assert outcome.plate == "KKR-6294"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CORRECTED
    assert outcome.matched is True
    assert outcome.reason == "plate_completed"
    assert outcome.guid == "FULL"
    assert outcome.reported_plate == "KKR-4"


@pytest.mark.asyncio
async def test_shadow_mode_reports_a_truncated_plate_without_changing_it(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "shadow")
    _stub_lookup(monkeypatch, _partial_pair())

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    # Shadow still changes nothing...
    assert outcome.plate == "KKR-4"
    assert outcome.plate_source == PLATE_SOURCE_EDGE_ANPR
    assert outcome.matched is False
    assert outcome.reason == "plate_partial_shadow"
    # ...but the fuller read is captured as the evidence.
    assert outcome.record.canonical_plate == "KKR-6294"


@pytest.mark.asyncio
async def test_a_short_plate_is_still_confirmed_when_nothing_fuller_exists(monkeypatch):
    """Short plates are legitimate. Without a fuller record of the same car, an
    agreeing short plate must confirm exactly as it always did."""
    _stub_lookup(monkeypatch, [_record(plate="4KKR", guid="ONLY")])

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    assert outcome.matched is True
    assert outcome.plate == "KKR-4"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CONFIRMED
    assert outcome.guid == "ONLY"


@pytest.mark.asyncio
async def test_a_fuller_record_outside_the_skew_window_is_ignored(monkeypatch):
    """Digit-compatibility is not enough on its own — a far-away pass is another
    car that happens to share a letter group."""
    monkeypatch.setattr(settings, "HIK_MATCH_MAX_SKEW_SECONDS", 10.0)
    _stub_lookup(
        monkeypatch,
        [
            _record(plate="6294KKR", guid="FAR", offset_seconds=-25),
            _record(plate="4KKR", guid="PARTIAL", offset_seconds=0),
        ],
    )

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    assert outcome.plate == "KKR-4"
    assert outcome.guid == "PARTIAL"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CONFIRMED


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


@pytest.mark.asyncio
async def test_a_fuller_record_beyond_the_partial_window_is_not_used(monkeypatch):
    """Completing a plate rewrites it in authoritative mode, so the fuller read
    must be close enough to be the SAME car's other frame — not merely inside
    the ordinary match skew."""
    monkeypatch.setattr(settings, "HIK_MATCH_MAX_SKEW_SECONDS", 10.0)
    monkeypatch.setattr(settings, "HIK_PARTIAL_MATCH_MAX_SKEW_SECONDS", 5.0)
    _stub_lookup(
        monkeypatch,
        [
            _record(plate="6294KKR", guid="FULL", offset_seconds=-8),
            _record(plate="4KKR", guid="PARTIAL", offset_seconds=0),
        ],
    )

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    assert outcome.plate == "KKR-4"
    assert outcome.guid == "PARTIAL"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CONFIRMED


@pytest.mark.asyncio
async def test_two_candidate_full_plates_are_ambiguous_and_change_nothing(monkeypatch):
    """Two different fuller plates in the window: nothing says which car the
    truncation came from, so the ANPR plate stands."""
    _stub_lookup(
        monkeypatch,
        [
            _record(plate="6294KKR", guid="FULL-A", offset_seconds=-3),
            _record(plate="1114KKR", guid="FULL-B", offset_seconds=-2),
            _record(plate="4KKR", guid="PARTIAL", offset_seconds=0),
        ],
    )

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    assert outcome.plate == "KKR-4"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CONFIRMED
    assert outcome.guid == "PARTIAL"


@pytest.mark.asyncio
async def test_the_same_fuller_plate_twice_is_not_ambiguous(monkeypatch):
    """Two records of ONE car (the platform saw it at two moments) still name a
    single plate, so the completion goes ahead."""
    _stub_lookup(
        monkeypatch,
        [
            _record(plate="6294KKR", guid="FULL-FAR", offset_seconds=-4),
            _record(plate="6294KKR", guid="FULL-NEAR", offset_seconds=-1),
            _record(plate="4KKR", guid="PARTIAL", offset_seconds=0),
        ],
    )

    outcome = await validation.validate_entry_plate("KKR-4", EVENT_TIME)

    assert outcome.plate == "KKR-6294"
    assert outcome.guid == "FULL-NEAR"
    assert outcome.reason == "plate_completed"


# ── Direction: the exit wrapper ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_validation_asks_the_exit_camera(monkeypatch):
    """The wrapper's whole job is pointing the same rules at the other camera.

    Worth an explicit assertion on the indexCode: `HIK_EXIT_RESOURCE_IDS` was
    `453`, which matches no camera, and crossRecords answers an unknown code with
    HTTP 200 / code=0 / an empty list — indistinguishable from a camera that
    genuinely saw nothing. Asking the ENTRY camera about an exit would fail the
    same silent way.
    """
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "510")
    calls = []
    _stub_lookup(monkeypatch, [_record(plate="5625JKA")], calls=calls)

    outcome = await validation.validate_exit_plate(ANPR_PLATE, EVENT_TIME)

    assert [c["resource_ids"] for c in calls] == ["510"]
    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_HIK_CONFIRMED


@pytest.mark.asyncio
async def test_exit_validation_corrects_a_misread_exit_plate(monkeypatch):
    """A wrong exit plate is not harmless — it is the evidence a session gets
    rewritten from, so it is corrected before anything downstream sees it."""
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "510")
    _stub_lookup(monkeypatch, [_record(plate="5625JKB", guid="EXIT-1")])

    outcome = await validation.validate_exit_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == "JKB-5625"
    assert outcome.plate_source == PLATE_SOURCE_HIK_CORRECTED
    assert outcome.reason == "plate_corrected"


@pytest.mark.asyncio
async def test_exit_validation_without_a_configured_camera_is_a_no_op(monkeypatch):
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "")

    async def explode(**kwargs):  # pragma: no cover
        raise AssertionError("must not query without a configured camera")

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", explode
    )

    outcome = await validation.validate_exit_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == ANPR_PLATE
    assert outcome.reason == "no_resource_ids_configured"


@pytest.mark.asyncio
async def test_shadow_mode_never_rewrites_an_exit_plate(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "shadow")
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "510")
    _stub_lookup(monkeypatch, [_record(plate="5625JKB", guid="EXIT-2")])

    outcome = await validation.validate_exit_plate(ANPR_PLATE, EVENT_TIME)

    assert outcome.plate == ANPR_PLATE
    assert outcome.plate_source == PLATE_SOURCE_EDGE_ANPR


# ── list_entry_candidates: anchor timezone discipline ───────────────────────
#
# These call the REAL function. Every other test of the Entry V2 enrichment
# path mocks `list_entry_candidates` itself, which is precisely why an anchor
# type mismatch between caller and callee shipped: nothing exercised the sort.


@pytest.mark.asyncio
async def test_entry_candidates_accepts_the_aware_anchor_entry_v2_sends(monkeypatch):
    """Entry V2 passes `_aware_trigger_time(event)`, which is always tz-aware.

    The sort subtracts the anchor from an always-naive `to_facility_naive(...)`,
    so an unconverted aware anchor raised TypeError — after the HikCentral call
    had already succeeded, so the records were fetched and then discarded while
    the caller reported `queried=False records=0`.
    """
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "447")
    _stub_lookup(monkeypatch, [_record(guid="AWARE-1", offset_seconds=5)])

    aware_anchor = EVENT_TIME.replace(tzinfo=FACILITY_TZ)
    records = await validation.list_entry_candidates(aware_anchor)

    assert [r.guid for r in records] == ["AWARE-1"]


@pytest.mark.asyncio
async def test_entry_candidates_still_accepts_the_naive_anchor_legacy_sends(monkeypatch):
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "447")
    _stub_lookup(monkeypatch, [_record(guid="NAIVE-1", offset_seconds=5)])

    records = await validation.list_entry_candidates(EVENT_TIME)

    assert [r.guid for r in records] == ["NAIVE-1"]


@pytest.mark.asyncio
async def test_entry_candidates_order_is_identical_for_both_anchor_types(monkeypatch):
    """Normalising must not silently shift the window or the |dt| ordering."""
    monkeypatch.setattr(settings, "HIK_ENTRY_RESOURCE_IDS", "447")
    spread = [
        _record(guid="FAR", offset_seconds=25),
        _record(guid="NEAR", offset_seconds=2),
        _record(guid="MID", offset_seconds=-9),
    ]

    _stub_lookup(monkeypatch, spread)
    naive_order = [r.guid for r in await validation.list_entry_candidates(EVENT_TIME)]

    _stub_lookup(monkeypatch, spread)
    aware_order = [
        r.guid
        for r in await validation.list_entry_candidates(
            EVENT_TIME.replace(tzinfo=FACILITY_TZ)
        )
    ]

    assert naive_order == ["NEAR", "MID", "FAR"]
    assert aware_order == naive_order
