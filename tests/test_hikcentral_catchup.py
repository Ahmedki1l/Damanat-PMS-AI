"""Restart catch-up: the reconcile window is anchored on the last consumed
HikCentral pass, not on a fixed lookback.

The 2026-08-09 incident this guards against: PMS-AI stopped ingesting for ~4h,
25 HikCentral exits landed in the hole, and the 15-minute rolling window could
only see the tail of it when the service came back. Every stranded session
surfaced on the dashboard as a 24h+ overstay for a car that had driven home.
"""

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models.hik_validation import HikValidation
from app.services import entry_exit_service as ees
from app.services import hikcentral


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
