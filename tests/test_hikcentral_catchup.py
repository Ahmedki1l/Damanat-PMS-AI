"""Restart catch-up: the reconcile window is anchored on the last consumed
HikCentral pass, not on a fixed lookback.

The 2026-08-09 incident this guards against: PMS-AI stopped ingesting for ~4h,
25 HikCentral exits landed in the hole, and the 15-minute rolling window could
only see the tail of it when the service came back. Every stranded session
surfaced on the dashboard as a 24h+ overstay for a car that had driven home.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.models.hik_validation import HikValidation
from app.services import entry_exit_service as ees
from app.services import hikcentral

FTZ = timezone(timedelta(hours=3))


@pytest.fixture
def db_session():
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


def _mark(db, direction, pass_time):
    db.add(HikValidation(
        direction=direction, guid=f"g-{direction}-{pass_time.isoformat()}",
        plate_source="hik_confirmed", pass_time=pass_time,
        matched=True, created_at=pass_time,
    ))
    db.commit()


# ── the window itself ───────────────────────────────────────────────────────


def test_window_reaches_back_to_the_last_consumed_pass(db_session, monkeypatch):
    """A 4h gap must produce a 4h window — not a 15-minute one."""
    now = datetime(2026, 8, 9, 18, 21, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_RECONCILE_LOOKBACK_SECONDS", 900.0)
    monkeypatch.setattr(settings, "HIK_CATCHUP_MAX_HOURS", 24.0)
    _mark(db_session, hikcentral.DIRECTION_EXIT, now - timedelta(hours=4))

    begin, _ = ees._reconcile_window(db_session, hikcentral.DIRECTION_EXIT)

    assert begin == now - timedelta(hours=4), (
        "the sweep must resume where the last consumed pass left off; a fixed "
        "lookback is what stranded the 2026-08-09 exits"
    )


def test_lookback_is_a_floor_when_the_watermark_is_recent(db_session, monkeypatch):
    """A watermark from 60s ago must not shrink the window below the lookback —
    the rolling cover for records the edge is still processing stays intact."""
    now = datetime(2026, 8, 9, 18, 21, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_RECONCILE_LOOKBACK_SECONDS", 900.0)
    _mark(db_session, hikcentral.DIRECTION_EXIT, now - timedelta(seconds=60))

    begin, _ = ees._reconcile_window(db_session, hikcentral.DIRECTION_EXIT)

    assert begin == now - timedelta(seconds=900)


def test_window_is_capped_so_an_old_backup_cannot_sweep_forever(db_session, monkeypatch):
    now = datetime(2026, 8, 9, 18, 21, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_CATCHUP_MAX_HOURS", 24.0)
    _mark(db_session, hikcentral.DIRECTION_EXIT, now - timedelta(days=9))

    begin, _ = ees._reconcile_window(db_session, hikcentral.DIRECTION_EXIT)

    assert begin == now - timedelta(hours=24)


def test_empty_table_falls_back_to_the_rolling_lookback(db_session, monkeypatch):
    now = datetime(2026, 8, 9, 18, 21, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_RECONCILE_LOOKBACK_SECONDS", 900.0)

    begin, _ = ees._reconcile_window(db_session, hikcentral.DIRECTION_EXIT)

    assert begin == now - timedelta(seconds=900)


def test_directions_carry_independent_watermarks(db_session, monkeypatch):
    """An exit sweep must not be dragged back by an old entry pass."""
    now = datetime(2026, 8, 9, 18, 21, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_RECONCILE_LOOKBACK_SECONDS", 900.0)
    _mark(db_session, hikcentral.DIRECTION_ENTRY, now - timedelta(hours=6))
    _mark(db_session, hikcentral.DIRECTION_EXIT, now - timedelta(hours=2))

    exit_begin, _ = ees._reconcile_window(db_session, hikcentral.DIRECTION_EXIT)
    entry_begin, _ = ees._reconcile_window(db_session, hikcentral.DIRECTION_ENTRY)

    assert exit_begin == now - timedelta(hours=2)
    assert entry_begin == now - timedelta(hours=6)


# ── chunking + concurrency ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_long_gap_is_swept_in_chunks(db_session, monkeypatch):
    """query_vehicle_logs does not paginate — it takes page 1 newest-first — so
    a single 4h request would silently drop the OLDEST records, which are
    exactly the ones a catch-up exists to find."""
    now = datetime(2026, 8, 9, 18, 21, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_CATCHUP_CHUNK_MINUTES", 30.0)
    monkeypatch.setattr(settings, "HIK_RECONCILE_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(ees, "SessionLocal", lambda: db_session, raising=False)
    monkeypatch.setattr("app.database.SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None, raising=False)
    _mark(db_session, hikcentral.DIRECTION_EXIT, now - timedelta(hours=4))

    windows = []

    async def fake_sweep(db, window=None):
        windows.append(window)

    monkeypatch.setattr(ees, "_reconcile_missed_exits", fake_sweep)
    ees._sweep_in_flight.clear()

    await ees._run_reconcile(hikcentral.DIRECTION_EXIT)

    assert len(windows) == 8, f"4h at 30min chunks = 8 requests, got {len(windows)}"
    assert windows[0][0] == now - timedelta(hours=4)
    assert windows[-1][1] == now
    # contiguous, no holes
    for earlier, later in zip(windows, windows[1:]):
        assert earlier[1] == later[0]


# ── the watermark must advance on a HEALTHY sweep, not only on a repair ─────


def _hik_record(guid, plate, cross_time):
    from app.services.hikcentral.models import VehicleLogRecord

    return VehicleLogRecord.from_openapi_record({
        "crossRecordSyscode": guid,
        "cameraIndexCode": "510",
        "plateNo": plate,
        "crossTime": cross_time.replace(tzinfo=FTZ).isoformat(),
        "vehiclePicUri": "Vsm://v",
    })


def _edge_logged(db, plate, when, gate):
    from app.models.entry_exit_log import EntryExitLog

    db.add(EntryExitLog(
        plate_number=plate, gate=gate, event_time=when, camera_id="CAM-EXIT",
    ))
    db.commit()


@pytest.mark.asyncio
async def test_a_healthy_exit_sweep_advances_the_watermark(db_session, monkeypatch):
    """The 2026-08-11..16 outage: every exit the sweep saw was already logged by
    the edge, so it consumed nothing, so `MAX(pass_time)` never moved. The
    watermark froze at the last repair, drifted past HIK_CATCHUP_MAX_HOURS, and
    every later sweep re-walked the whole cap window and found nothing — while a
    genuine gap older than the cap became permanently unreachable. Five days of
    exits were never recovered.

    A sweep that examines a pass and finds nothing to do must still consume it.
    """
    now = datetime(2026, 8, 16, 9, 0, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)

    pass_time = now - timedelta(hours=2)
    # The edge already logged this exit — the sweep has nothing to repair.
    _edge_logged(db_session, "JKA-5625", pass_time, "exit")

    async def fake_list(resource_ids, begin, end, db):
        return [_hik_record("G-1", "5625JKA", pass_time)]

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)

    assert ees._catchup_watermark(db_session, hikcentral.DIRECTION_EXIT) is None

    await ees._reconcile_missed_exits(db_session, window=(now - timedelta(hours=3), now))

    assert ees._catchup_watermark(db_session, hikcentral.DIRECTION_EXIT) == pass_time, (
        "an already-logged pass must still be consumed, or the watermark freezes "
        "and every later sweep re-walks the full cap window for nothing"
    )


@pytest.mark.asyncio
async def test_a_consumed_pass_is_not_re_examined_by_the_next_sweep(db_session, monkeypatch):
    """Consuming is what makes overlapping windows cheap: the same pass must drop
    out of `list_unconsumed_records` on every later sweep."""
    from app.services.hikcentral import validation

    now = datetime(2026, 8, 16, 9, 0, 0)
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    pass_time = now - timedelta(hours=2)
    record = _hik_record("G-1", "5625JKA", pass_time)

    assert validation.consume_already_logged(
        db_session, record, hikcentral.DIRECTION_EXIT
    ) == "G-1"
    db_session.commit()

    assert validation.guid_already_used(db_session, "G-1") is True
    # Idempotent: a second sweep seeing the same record must not double-write.
    assert validation.consume_already_logged(
        db_session, record, hikcentral.DIRECTION_EXIT
    ) is None


@pytest.mark.asyncio
async def test_a_genuinely_missed_exit_is_still_repaired_not_just_consumed(
    db_session, monkeypatch
):
    """The consume path must never swallow a pass the sweep should have acted on —
    that would turn a stranded session into a permanently stranded one."""
    from app.models.entry_exit_log import EntryExitLog

    now = datetime(2026, 8, 16, 9, 0, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)

    pass_time = now - timedelta(hours=2)
    # No edge log at all — this car's exit was missed entirely.

    async def fake_list(resource_ids, begin, end, db):
        return [_hik_record("G-2", "5625JKA", pass_time)]

    async def fake_images(outcome):
        from app.services.hikcentral.models import HikImages
        return HikImages()

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)
    monkeypatch.setattr(hikcentral, "download_hik_images", fake_images)

    await ees._reconcile_missed_exits(db_session, window=(now - timedelta(hours=3), now))

    rows = db_session.query(EntryExitLog).filter(EntryExitLog.gate == "exit").all()
    assert len(rows) == 1 and rows[0].plate_number == "JKA-5625", (
        "a missed exit must still produce its audit row"
    )
    marks = db_session.query(HikValidation).filter(
        HikValidation.guid == "G-2"
    ).all()
    assert len(marks) == 1 and marks[0].match_reason != "edge_already_logged", (
        "a repaired pass must be recorded as a repair, not as already-logged"
    )


@pytest.mark.asyncio
async def test_shadow_mode_consumes_already_logged_but_not_missed(
    db_session, monkeypatch
):
    """Shadow suffers the identical freeze, so it must consume already-logged
    passes too — but a pass it only WOULD have acted on stays unconsumed, so
    flipping to authoritative can still repair it."""
    now = datetime(2026, 8, 16, 9, 0, 0)
    monkeypatch.setattr(ees, "facility_now_naive", lambda: now)
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "shadow")
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)

    seen = now - timedelta(hours=2)
    missed = now - timedelta(hours=1)
    _edge_logged(db_session, "JKA-5625", seen, "exit")

    async def fake_list(resource_ids, begin, end, db):
        return [
            _hik_record("G-SEEN", "5625JKA", seen),
            _hik_record("G-MISSED", "1111ZZT", missed),
        ]

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)

    await ees._reconcile_missed_exits(db_session, window=(now - timedelta(hours=3), now))

    guids = {g for (g,) in db_session.query(HikValidation.guid).all()}
    assert guids == {"G-SEEN"}, (
        "shadow must consume the already-logged pass and leave the missed one "
        f"for authoritative mode to repair; got {guids}"
    )


@pytest.mark.asyncio
async def test_a_second_sweep_yields_to_the_one_already_running(monkeypatch):
    """A multi-hour catch-up outlives the 30s debounce; overlapping sweeps would
    re-query the same span and double the platform load for nothing."""
    ees._sweep_in_flight.clear()
    ees._sweep_in_flight.add(hikcentral.DIRECTION_EXIT)
    called = []
    monkeypatch.setattr("app.database.SessionLocal", lambda: called.append(1))

    await ees._run_reconcile(hikcentral.DIRECTION_EXIT)

    assert called == [], "the second sweep must not even open a session"
    ees._sweep_in_flight.clear()
