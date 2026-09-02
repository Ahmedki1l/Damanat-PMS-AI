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
    ees._UNVERIFIED_REFUSALS.clear()
    yield
    ees._entry_bursts.clear()
    ees._pending_crossings.clear()
    ees._recent_entries.clear()
    ees._UNVERIFIED_REFUSALS.clear()


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
async def test_lookup_failure_is_signalled_not_swallowed(db, monkeypatch):
    """POLICY CHANGE. This used to assert `is None`, i.e. a failed lookup was
    reported as "nothing to consume".

    Those are different facts and collapsing them fails OPEN: SUZ-975's lookup
    timed out, the refusal was logged as "nothing for the reconciler to
    re-open", and the reconciler opened that very record 32 minutes later. The
    low-level call now says which one happened; keeping the flusher alive is the
    caller's job, asserted in the flusher tests below.
    """

    async def boom(*a, **k):
        raise RuntimeError("HikCentral unreachable")

    monkeypatch.setattr(client, "query_vehicle_logs", boom)

    with pytest.raises(validation.RefusalLookupFailed):
        await validation.consume_refused_entry(db, "66565EK", READ_TIME)
    assert db.query(HikValidation).count() == 0


@pytest.mark.asyncio
async def test_verified_empty_lookup_still_reports_nothing_to_consume(db, monkeypatch):
    """"We asked and there is nothing" must stay distinguishable from "we could
    not ask" — it is still a plain None, and holds nothing off the reconciler."""

    async def empty(*a, **k):
        return []

    monkeypatch.setattr(client, "query_vehicle_logs", empty)

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


# ── unverified refusals: the SUZ-975 leak ───────────────────────────────────


def _explode_lookup(monkeypatch):
    async def boom(begin, end, resource_ids, page_size):
        raise RuntimeError("HikCentral read timeout")

    monkeypatch.setattr(client, "query_vehicle_logs", boom)


@pytest.mark.asyncio
async def test_unverified_refusal_holds_the_reconciler_off(db, monkeypatch):
    """Reproduces the live SUZ-975 sequence of 2026-08-31.

        07:33:17  burst buffered plate=SUZ-975
        07:34:23  refused; lookup FAILED, logged "nothing to re-open"
        08:06:33  [Hik][reconcile] OPENED missed entry     <- the leak

    A refusal we could not prove must not be treated as a refusal with nothing
    behind it. The sweep must leave the pass alone.
    """
    ees._entry_bursts[1] = _burst()
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    _explode_lookup(monkeypatch)
    await ees.flush_due_entry_bursts(db)        # refuse; tombstone FAILS
    assert len(ees._UNVERIFIED_REFUSALS) == 1

    # HikCentral comes back, and now returns the record the timeout hid.
    _patch_lookup(monkeypatch, [_record()])
    await ees._reconcile_missed_entries(db)

    assert db.query(ParkingSession).count() == 0
    assert db.query(HikValidation).filter(HikValidation.matched.is_(True)).count() == 0


@pytest.mark.asyncio
async def test_retry_tombstones_the_refusal_once_hikcentral_answers(db, monkeypatch):
    """The hold is not permanent — the next sweep makes the refusal durable."""
    ees._entry_bursts[1] = _burst()
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    _explode_lookup(monkeypatch)
    await ees.flush_due_entry_bursts(db)
    assert len(ees._UNVERIFIED_REFUSALS) == 1

    _patch_lookup(monkeypatch, [_record()])
    await ees._reconcile_missed_entries(db)

    # Consumed as a refusal, so the hold is released and the GUID is spent.
    assert ees._UNVERIFIED_REFUSALS == []
    row = db.query(HikValidation).filter(HikValidation.guid == "G1").one()
    assert row.matched is False
    assert row.match_reason == validation.REFUSED_NO_CROSSING


@pytest.mark.asyncio
async def test_an_empty_lookup_holds_the_pass_for_a_bounded_time(db, monkeypatch):
    """"We asked, HikCentral has nothing" is not proof that nothing exists.

    This used to release immediately, on the reasoning that a refusal at a gate
    HikCentral does not cover would otherwise blind the sweep for the life of
    the process. That reasoning was sound; the conclusion was not. crossRecords
    lags, so at refusal time the record for the pass we just refused is exactly
    the one most likely to be missing — and it surfaces minutes later carrying a
    pass_time from BEFORE the refusal. See the RGR-6666 test below.

    The hold answers the original objection with a deadline instead of an
    immediate release: bounded by HIK_REFUSAL_HOLD_SECONDS, never permanent.
    """
    ees._entry_bursts[1] = _burst()
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    _patch_lookup(monkeypatch, [])
    await ees.flush_due_entry_bursts(db)

    assert len(ees._UNVERIFIED_REFUSALS) == 1


@pytest.mark.asyncio
async def test_the_block_does_not_expire(db, monkeypatch):
    """An ANPR read with no ramp crossing is not a car, and does not become one.

    HIK_REFUSAL_HOLD_SECONDS bounds the tombstone RETRIES, not the block. Past
    it we stop asking HikCentral and the pass stays refused.
    """
    monkeypatch.setattr(settings, "HIK_REFUSAL_HOLD_SECONDS", 900.0)
    ees._entry_bursts[1] = _burst()
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )
    _patch_lookup(monkeypatch, [])
    await ees.flush_due_entry_bursts(db)
    assert len(ees._UNVERIFIED_REFUSALS) == 1

    # Long past the retry window, and now HikCentral does have the record.
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=12000)
    )
    _patch_lookup(monkeypatch, [_record()])
    monkeypatch.setattr(ees, "_flush_entry_burst", AsyncMock())

    await ees._reconcile_missed_entries(db)

    ees._flush_entry_burst.assert_not_awaited()
    assert db.query(ParkingSession).count() == 0
    assert len(ees._UNVERIFIED_REFUSALS) == 1      # still blocking


@pytest.mark.asyncio
async def test_a_late_arriving_record_cannot_reopen_a_refused_pass(db, monkeypatch):
    """Reproduces the live RGR-6666 sequence of 2026-09-02.

        09:14:59  ANPR read #1 buffered            plate=RGR-6666
        09:15:16  ANPR read #2 buffered            plate=RGR-6466  (same car)
        09:15:31  ramp crossing -> read #2 wins, RGR-6466 admitted
        09:15:58  read #1 DROPPED at 59s           <- correct refusal
        09:16:09  tombstone lookup returns EMPTY   <- "nothing to re-open"
        09:22:36  [Hik][reconcile] OPENED missed entry RGR-6666   <- the bug

    The opened record's pass_time was 09:14:55 — six minutes BEFORE the refusal
    that was supposed to suppress it. It became a second session and a second
    unregistered-vehicle alert for a car already parked as RGR-6466.

    An empty lookup is a lag, not an absence. The sweep must find nothing to
    open once the record finally surfaces.
    """
    monkeypatch.setattr(settings, "HIK_REFUSAL_HOLD_SECONDS", 900.0)
    ees._entry_bursts[1] = _burst(plate="RGR-6666")
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    # 09:16:09 — the tombstone lookup comes back empty.
    _patch_lookup(monkeypatch, [])
    await ees.flush_due_entry_bursts(db)
    assert len(ees._UNVERIFIED_REFUSALS) == 1

    # 09:22:36 — the record surfaces, stamped BEFORE the refusal, and the sweep
    # runs. It must be tombstoned on the retry, not opened.
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=460)
    )
    _patch_lookup(monkeypatch, [_record(guid="RGR6666GUID", plate="RGR-6666")])
    monkeypatch.setattr(ees, "_flush_entry_burst", AsyncMock())

    await ees._reconcile_missed_entries(db)

    ees._flush_entry_burst.assert_not_awaited()
    assert db.query(ParkingSession).count() == 0


@pytest.mark.asyncio
async def test_a_stale_watermark_outlives_a_short_hold(db, monkeypatch):
    """Reproduces USB-6662, 2026-09-02 — why the hold is 24h and not 15 minutes.

        12:48:04  Hik pass_time
        12:49:15  refused; tombstone lookup EMPTY; held 900s -> 13:04:15
        13:04:15  hold expires; nothing sweeps at that moment
        16:15:34  sweep runs. Entry watermark still frozen at 12:14:13, so it
                  logs "3:59:21 gap since the last consumed pass" and re-walks
                  a span CONTAINING 12:48:04
        16:15:55  [Hik][reconcile] OPENED missed entry USB-6662   <- the bug

    No timed block can fix this. _reconcile_window returns
    min(now - lookback, watermark), so a stale watermark re-walks arbitrarily
    far back — there is no point at which the pass is out of reach. The block
    has to be unconditional, which is what "no crossing, no entry" means.
    """
    monkeypatch.setattr(settings, "HIK_REFUSAL_HOLD_SECONDS", 900.0)
    ees._entry_bursts[1] = _burst(plate="USB-6662")
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )
    _patch_lookup(monkeypatch, [])
    await ees.flush_due_entry_bursts(db)
    assert len(ees._UNVERIFIED_REFUSALS) == 1

    # 3h27m later — the gap USB-6662 actually slipped through, off a sweep whose
    # watermark had not moved since before the refusal.
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=12540)
    )
    _patch_lookup(monkeypatch, [_record(guid="USBGUID", plate="USB-6662")])
    monkeypatch.setattr(ees, "_flush_entry_burst", AsyncMock())

    await ees._reconcile_missed_entries(db)

    ees._flush_entry_burst.assert_not_awaited()
    assert db.query(ParkingSession).count() == 0


@pytest.mark.asyncio
async def test_hold_only_covers_the_refused_car(db, monkeypatch):
    """A different car passing while a refusal is unverified still gets opened."""
    ees._entry_bursts[1] = _burst()
    monkeypatch.setattr(
        ees, "facility_now_naive", lambda: READ_TIME + timedelta(seconds=120)
    )

    _explode_lookup(monkeypatch)
    await ees.flush_due_entry_bursts(db)
    assert len(ees._UNVERIFIED_REFUSALS) == 1

    opened = []

    async def fake_flush(db_, buf):
        opened.append(buf["reads"][0]["plate"])

    monkeypatch.setattr(ees, "_flush_entry_burst", fake_flush)
    _patch_lookup(monkeypatch, [_record(guid="G2", plate="9999XYZ")])
    await ees._reconcile_missed_entries(db)

    assert opened == ["XYZ-9999"]


# ── imageless recoveries: the unclosable session ────────────────────────────


def _record_without_image(guid="G3", plate="05826LD"):
    return VehicleLogRecord.from_openapi_record({
        "crossRecordSyscode": guid,
        "cameraIndexCode": "447",
        "plateNo": plate,
        "crossTime": "2026-07-30T05:30:01+03:00",
    })


@pytest.mark.asyncio
async def test_reconciler_refuses_to_open_an_imageless_entry(db, monkeypatch):
    """Reproduces 05826LD of 2026-08-30.

        [UC1] Flushing entry burst winner=05826LD pic=None conf=None
        [PMS] No snapshot available for plate=05826LD - sending without image

    The plate is a misread no car will present at the exit, and with no image
    the exit Re-ID fallback has nothing to match — so the session can never
    close. It was still open, accruing stay time, days later.
    """
    _patch_lookup(monkeypatch, [_record_without_image()])

    await ees._reconcile_missed_entries(db)

    assert db.query(ParkingSession).count() == 0


@pytest.mark.asyncio
async def test_an_imageless_guid_is_left_unconsumed_for_a_later_sweep(db, monkeypatch):
    """Refusing is not the same as tombstoning — the pass may reappear with
    imagery, and consuming it here would make that unrecoverable."""
    _patch_lookup(monkeypatch, [_record_without_image()])

    await ees._reconcile_missed_entries(db)

    assert db.query(HikValidation).filter(HikValidation.guid == "G3").count() == 0


@pytest.mark.asyncio
async def test_a_record_with_an_image_still_opens(db, monkeypatch):
    """The guard must only exclude records that could never be closed."""
    _patch_lookup(monkeypatch, [_record()])
    opened = []

    async def fake_flush(db_, buf):
        opened.append(buf["reads"][0]["plate"])

    monkeypatch.setattr(ees, "_flush_entry_burst", fake_flush)
    await ees._reconcile_missed_entries(db)

    assert opened == ["66565EK"]


@pytest.mark.asyncio
async def test_the_guard_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "HIK_RECONCILE_REQUIRE_IMAGE", False)
    _patch_lookup(monkeypatch, [_record_without_image()])
    opened = []

    async def fake_flush(db_, buf):
        opened.append(buf["reads"][0]["plate"])

    monkeypatch.setattr(ees, "_flush_entry_burst", fake_flush)
    await ees._reconcile_missed_entries(db)

    assert opened == ["05826LD"]
