"""Event-driven HikCentral reconciliation: trigger routing, debounce, and the
'skip already-consumed' rule that makes overlapping sweeps idempotent."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import entry_exit_service as ees
from app.services import hikcentral
from app.services.hikcentral import client, validation
from app.services.hikcentral.models import VehicleLogRecord

FTZ = timezone(timedelta(hours=3))


def _record(guid, plate="5625JKA"):
    return VehicleLogRecord.from_openapi_record({
        "crossRecordSyscode": guid, "cameraIndexCode": "447", "plateNo": plate,
        "crossTime": "2026-07-28T14:20:16+03:00", "vehiclePicUri": "Vsm://v",
    })


# ── Trigger routing + debounce ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_note_gate_event_routes_by_camera_and_debounces(monkeypatch):
    monkeypatch.setattr(hikcentral, "is_enabled", lambda: True)
    monkeypatch.setattr(settings, "HIK_RECONCILE_ENTRY_TRIGGER_CAMERAS", "CAM-23,CAM-03")
    monkeypatch.setattr(settings, "HIK_RECONCILE_EXIT_TRIGGER_CAMERAS", "CAM-08")
    monkeypatch.setattr(settings, "HIK_RECONCILE_DEBOUNCE_SECONDS", 999.0)
    ees._last_reconcile_at.clear()

    fired = []

    async def fake_run(direction):
        fired.append(direction)

    monkeypatch.setattr(ees, "_run_reconcile", fake_run)

    ees.note_gate_event("CAM-23")   # entry
    ees.note_gate_event("CAM-08")   # exit
    ees.note_gate_event("CAM-03")   # entry again — debounced away
    ees.note_gate_event("CAM-99")   # not a trigger camera
    await asyncio.sleep(0)          # let the spawned tasks run

    assert sorted(fired) == ["entry", "exit"]


@pytest.mark.asyncio
async def test_note_gate_event_is_a_noop_when_layer_is_off(monkeypatch):
    monkeypatch.setattr(hikcentral, "is_enabled", lambda: False)
    ees._last_reconcile_at.clear()
    fired = []
    monkeypatch.setattr(ees, "_run_reconcile",
                        lambda d: fired.append(d))
    ees.note_gate_event("CAM-23")
    await asyncio.sleep(0)
    assert fired == []


# ── Candidate selection ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_unconsumed_records_drops_used_guids_and_plateless(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_RECONCILE_PAGE_SIZE", 100)
    recs = [_record("A"), _record("B"), _record("C")]

    async def fake_query(begin, end, resource_ids, page_size):
        assert resource_ids == "447"
        return recs

    monkeypatch.setattr(client, "query_vehicle_logs", fake_query)
    # A is already consumed by an earlier session/sweep.
    monkeypatch.setattr(validation, "guid_already_used", lambda db, g: g == "A")

    begin = datetime(2026, 7, 28, 14, 0, tzinfo=FTZ)
    end = datetime(2026, 7, 28, 14, 30, tzinfo=FTZ)
    out = await validation.list_unconsumed_records("447", begin, end, None)

    assert [r.guid for r in out] == ["B", "C"]


@pytest.mark.asyncio
async def test_list_unconsumed_records_empty_when_off(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")
    called = []

    async def fake_query(*a, **k):
        called.append(1)
        return []

    monkeypatch.setattr(client, "query_vehicle_logs", fake_query)
    out = await validation.list_unconsumed_records(
        "447", datetime.now(FTZ), datetime.now(FTZ), None
    )
    assert out == [] and called == []


# ── The sweep must not re-open a car the edge logged under a fuller plate ────


@pytest.fixture
def recon_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


WHEN = datetime(2026, 8, 9, 9, 27, 19)
MATCH_WINDOW = timedelta(seconds=30)


def _log_entry(db, plate, when=WHEN, gate="entry"):
    from app.models.entry_exit_log import EntryExitLog

    db.add(EntryExitLog(plate_number=plate, gate=gate, event_time=when, camera_id="CAM-ENTRY"))
    db.commit()


def test_leftover_truncated_record_counts_as_already_logged(recon_db):
    """The 2026-08-09 pair: the burst merges 6294KKR + 4KKR and writes ONE entry
    as KKR-6294, so HikCentral's KKR-4 row is left unconsumed. The sweep must
    recognise it as the same pass, not open a phantom session."""
    _log_entry(recon_db, "KKR-6294")

    assert ees._gate_event_already_logged(recon_db, "KKR-4", "entry", WHEN, MATCH_WINDOW) is True


def test_exact_plate_still_counts_as_already_logged(recon_db):
    _log_entry(recon_db, "KKR-6294")

    assert ees._gate_event_already_logged(recon_db, "KKR-6294", "entry", WHEN, MATCH_WINDOW) is True


def test_a_genuinely_missed_car_is_still_reported(recon_db):
    """An unrelated plate in the window must NOT be suppressed — that is the
    whole point of the sweep."""
    _log_entry(recon_db, "KKR-6294")

    assert ees._gate_event_already_logged(recon_db, "ZZT-1111", "entry", WHEN, MATCH_WINDOW) is False


def test_the_other_gate_and_far_passes_do_not_suppress(recon_db):
    _log_entry(recon_db, "KKR-6294", gate="exit")
    _log_entry(recon_db, "KKR-6294", when=WHEN - timedelta(minutes=5))

    assert ees._gate_event_already_logged(recon_db, "KKR-4", "entry", WHEN, MATCH_WINDOW) is False
