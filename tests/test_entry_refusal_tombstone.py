"""A burst the crossing gate refused must stay refused.

The entry pipeline only writes an entry once a ramp crossing (CAM-23/CAM-03)
physically confirms a car. An unconfirmed burst is dropped and — deliberately —
leaves no `EntryExitLog`.

That is exactly the state `_reconcile_missed_entries` reads as "the edge missed
this car", so before the tombstone the reconciler re-opened every entry the gate
refused, a few minutes later, under `plate_source=hik_polled`. Those sessions had
no exit to close them and surfaced on the dashboard as overstays.

`test_reconciler_cannot_reopen_a_refused_burst` reproduces that sequence
end-to-end and is the regression guard for it.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.services import entry_exit_service as ees
from app.services.hikcentral import client, validation
from app.services.hikcentral.models import VehicleLogRecord

FTZ = timezone(timedelta(hours=3))
# Naive facility-local, matching what the burst buffer stores.
READ_TIME = datetime(2026, 7, 30, 5, 30, 1)

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
    monkeypatch.setattr(settings, "ANPR_BURST_MAX_SECONDS", 60.0)
    monkeypatch.setattr(settings, "USE_CAM03_ENTRY_CONFIRMATION", True)
    monkeypatch.setattr(
        "app.utils.core_backend_client.notify_pms_anpr", AsyncMock()
    )
    ees._entry_bursts.clear()
    ees._pending_crossings.clear()
    ees._recent_entries.clear()
    yield
    ees._entry_bursts.clear()
    ees._pending_crossings.clear()
    ees._recent_entries.clear()


def _record(guid="G1", plate="66565EK", when="2026-07-30T05:30:01+03:00"):
    return VehicleLogRecord.from_openapi_record({
        "crossRecordSyscode": guid,
        "cameraIndexCode": "447",
        "plateNo": plate,
        "crossTime": when,
        "vehiclePicUri": "Vsm://v",
    })


def _patch_lookup(monkeypatch, records):
    async def fake_query(begin, end, resource_ids, page_size):
        return list(records)

    monkeypatch.setattr(client, "query_vehicle_logs", fake_query)


def _burst(plate="66565EK", pic_num=3, camera_id="CAM-ENTRY"):
    """A buffered, never-confirmed entry burst, shaped like the live one."""
    return {
        "id": 1,
        "camera_id": camera_id,
        "reads": [{
            "plate": plate,
            "confidence": 61,
            "pic_num": pic_num,
            "event_time": READ_TIME,
            "snapshot_path": None,
            "local_snapshot_path": None,
        }],
        "first_event_time": READ_TIME,
        "last_read_at": READ_TIME,
        "confirmed": False,
        "confirm_snapshots": {},
        "confirm_source": None,
        "force_flush": False,
    }


# ── consume_refused_entry ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consume_refused_entry_writes_an_unmatched_tombstone(db, monkeypatch):
    _patch_lookup(monkeypatch, [_record()])

    guid = await validation.consume_refused_entry(db, "66565EK", READ_TIME)
    db.flush()

    assert guid == "G1"
    row = db.query(HikValidation).one()
    assert row.guid == "G1"
    assert row.matched is False
    assert row.match_reason == validation.REFUSED_NO_CROSSING
    assert row.direction == validation.DIRECTION_ENTRY
    # A refusal justifies no session and no gate-log row.
    assert row.session_id is None
    assert row.entry_exit_log_id is None


@pytest.mark.asyncio
async def test_consumed_guid_is_invisible_to_the_reconcile_sweep(db, monkeypatch):
    """The whole mechanism: consuming the GUID is what hides it from the sweep."""
    rec = _record()
    _patch_lookup(monkeypatch, [rec])

    before = await validation.list_unconsumed_records(
        "447", READ_TIME - timedelta(minutes=15), READ_TIME, db
    )
    assert [r.guid for r in before] == ["G1"]

    await validation.consume_refused_entry(db, "66565EK", READ_TIME)
    db.flush()

    after = await validation.list_unconsumed_records(
        "447", READ_TIME - timedelta(minutes=15), READ_TIME, db
    )
    assert after == []


@pytest.mark.asyncio
async def test_no_hik_record_is_a_safe_no_op(db, monkeypatch):
    """Nothing to tombstone means nothing for the reconciler to act on either."""
    _patch_lookup(monkeypatch, [])

    assert await validation.consume_refused_entry(db, "66565EK", READ_TIME) is None
    assert db.query(HikValidation).count() == 0


@pytest.mark.asyncio
async def test_already_consumed_guid_is_not_double_written(db, monkeypatch):
    _patch_lookup(monkeypatch, [_record()])

    assert await validation.consume_refused_entry(db, "66565EK", READ_TIME) == "G1"
    db.flush()
    assert await validation.consume_refused_entry(db, "66565EK", READ_TIME) is None
    db.flush()

    assert db.query(HikValidation).count() == 1


@pytest.mark.asyncio
async def test_disabled_layer_issues_no_lookup(db, monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "off")
    called = []

    async def fake_query(*a, **k):
        called.append(1)
        return [_record()]

    monkeypatch.setattr(client, "query_vehicle_logs", fake_query)

    assert await validation.consume_refused_entry(db, "66565EK", READ_TIME) is None
    assert called == []


@pytest.mark.asyncio
async def test_lookup_failure_never_breaks_the_flusher(db, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("HikCentral unreachable")

    monkeypatch.setattr(client, "query_vehicle_logs", boom)

    assert await validation.consume_refused_entry(db, "66565EK", READ_TIME) is None
    assert db.query(HikValidation).count() == 0


# ── the burst drop path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tombstone_names_the_same_plate_the_flush_would_write(db, monkeypatch):
    """The burst is labelled by its highest-picNum read, not its first.

    If the tombstone used a different read, it would consume the GUID under one
    plate while the reconciler re-opened the pass under another.
    """
    buf = _burst(plate="EARLY-1", pic_num=1)
    buf["reads"].append({
        "plate": "66565EK", "confidence": 96, "pic_num": 3,
        "event_time": READ_TIME + timedelta(seconds=2),
        "snapshot_path": None, "local_snapshot_path": None,
    })

    assert ees._winning_read(buf["reads"])["plate"] == "66565EK"

    seen = {}

    async def fake_consume(db_, plate, event_time):
        seen["plate"] = plate
        return "G1"

    monkeypatch.setattr(ees.hikcentral, "consume_refused_entry", fake_consume)

    assert await ees._tombstone_refused_burst(db, buf) is True
    assert seen["plate"] == "66565EK"


@pytest.mark.asyncio
async def test_dropped_burst_is_tombstoned_by_the_flusher(db, monkeypatch):
    """An unconfirmed burst past its hard cap is dropped AND tombstoned."""
    _patch_lookup(monkeypatch, [_record()])
    ees._entry_bursts[1] = _burst()

    # Push the burst past ANPR_BURST_MAX_SECONDS so it is drop-eligible.
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    await ees.flush_due_entry_bursts(db)

    assert ees._entry_bursts == {}                       # dropped, not written
    assert db.query(ParkingSession).count() == 0         # no session opened
    row = db.query(HikValidation).one()                  # but the GUID is spent
    assert row.match_reason == validation.REFUSED_NO_CROSSING


@pytest.mark.asyncio
async def test_confirmed_burst_is_not_tombstoned(db, monkeypatch):
    """A burst the ramp DID confirm must take the normal write path."""
    _patch_lookup(monkeypatch, [_record()])
    buf = _burst()
    buf["confirmed"] = True
    buf["confirm_source"] = "CAM-23"
    ees._entry_bursts[1] = buf

    tombstoned = []
    monkeypatch.setattr(
        ees, "_tombstone_refused_burst",
        AsyncMock(side_effect=lambda *a: tombstoned.append(1) or True),
    )
    monkeypatch.setattr(ees, "_flush_entry_burst", AsyncMock())
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    await ees.flush_due_entry_bursts(db)

    assert tombstoned == []
    ees._flush_entry_burst.assert_awaited_once()


# ── the regression this whole change exists for ─────────────────────────────


@pytest.mark.asyncio
async def test_reconciler_cannot_reopen_a_refused_burst(db, monkeypatch):
    """Reproduces the live 66565EK sequence of 2026-07-30.

        05:30:01  burst buffered plate=66565EK conf=61
        05:30:54  burst DROPPED (no ramp confirmation)   <- correct refusal
        05:34:07  [Hik][reconcile] OPENED missed entry   <- the bug

    The sweep must now find nothing to open.
    """
    _patch_lookup(monkeypatch, [_record()])
    ees._entry_bursts[1] = _burst()
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    await ees.flush_due_entry_bursts(db)      # refuse + tombstone
    await ees._reconcile_missed_entries(db)   # the sweep that used to undo it

    assert db.query(ParkingSession).count() == 0
    # And no session-bearing validation row was written either.
    assert db.query(HikValidation).filter(HikValidation.matched.is_(True)).count() == 0


@pytest.mark.asyncio
async def test_reconciler_still_opens_a_genuinely_missed_entry(db, monkeypatch):
    """The guard must not blind the sweep to cars the edge really did miss."""
    _patch_lookup(monkeypatch, [_record()])
    opened = []

    async def fake_flush(db_, buf):
        opened.append(buf["reads"][0]["plate"])

    monkeypatch.setattr(ees, "_flush_entry_burst", fake_flush)

    await ees._reconcile_missed_entries(db)

    assert opened == ["66565EK"]
