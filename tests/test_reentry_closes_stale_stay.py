"""A confirmed re-entry must close the stay left open by a missed exit.

Production context (ai-logs, 2026-08-19..23): KXR-2538 exited on 08-20 16:17 and
the exit camera read its plate as "172538J". Nothing matched, the stay stayed
open, and when the car came back on 08-23 06:12 `open_session` REUSED that stay
— so the car read as never having left, and its three phantom neighbours
(AVD-4918, DIK-2, SHA-666) made up the entire reported garage occupancy.

The Entry V2 twin of this lives in test_entry_confirmations.py, but that path
only runs under ENTRY_V2_MODE=authoritative. These cover the legacy burst-flush
path, which is what actually opens stays in production today.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle  # noqa: F401 - registers FK target
from app.services import parking_session_service
from app.services.entry_exit_service import _reconcile_stale_stays_on_reentry
from app.services.parking_session_service import REENTRY_RECONCILIATION_CAMERA_ID

engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)

PLATE = "KXR-2538"
REENTRY = datetime(2026, 8, 23, 6, 12, 11)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def open_stay(db, entry_time, plate=PLATE):
    session = parking_session_service.open_session(
        db,
        plate_number=plate,
        event_time=entry_time,
        camera_id="CAM-ENTRY",
        snapshot_path=None,
    )
    db.flush()
    return session


def test_a_stay_left_open_by_a_missed_exit_is_closed_at_the_reentry(db):
    stale = open_stay(db, datetime(2026, 8, 20, 6, 10, 46))

    _reconcile_stale_stays_on_reentry(db, PLATE, REENTRY)

    db.refresh(stale)
    assert stale.status == "closed"
    assert stale.exit_time == REENTRY
    # The sentinel keeps this distinguishable from a real exit-camera read.
    assert stale.exit_camera_id == REENTRY_RECONCILIATION_CAMERA_ID
    assert stale.duration_seconds == int(
        (REENTRY - datetime(2026, 8, 20, 6, 10, 46)).total_seconds()
    )


def test_the_reentry_then_opens_a_fresh_stay_instead_of_reusing_the_stale_one(db):
    stale = open_stay(db, datetime(2026, 8, 20, 6, 10, 46))

    _reconcile_stale_stays_on_reentry(db, PLATE, REENTRY)
    fresh = open_stay(db, REENTRY)

    assert fresh.id != stale.id
    assert fresh.entry_time == REENTRY
    assert fresh.status == "open"
    open_stays = (
        db.query(ParkingSession)
        .filter(ParkingSession.status == "open")
        .all()
    )
    assert [row.id for row in open_stays] == [fresh.id]


def test_a_stay_opened_moments_ago_is_left_alone(db):
    """The guard against churning a double-fire the 30s dedup did not absorb."""
    recent = open_stay(db, REENTRY - timedelta(seconds=30))

    _reconcile_stale_stays_on_reentry(db, PLATE, REENTRY)

    db.refresh(recent)
    assert recent.status == "open"
    assert recent.exit_time is None


def test_another_cars_stay_is_never_touched(db):
    mine = open_stay(db, datetime(2026, 8, 20, 6, 10, 46))
    theirs = open_stay(db, datetime(2026, 8, 20, 7, 0, 0), plate="XRD-6663")

    _reconcile_stale_stays_on_reentry(db, PLATE, REENTRY)

    db.refresh(mine)
    db.refresh(theirs)
    assert mine.status == "closed"
    assert theirs.status == "open"


def test_the_feature_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_REENTRY_RECONCILE_ENABLED", False)
    stale = open_stay(db, datetime(2026, 8, 20, 6, 10, 46))

    _reconcile_stale_stays_on_reentry(db, PLATE, REENTRY)

    db.refresh(stale)
    assert stale.status == "open"


def test_a_failure_to_reconcile_never_costs_the_caller_its_entry(db, monkeypatch):
    """Degrade, not raise: the worst case is the status quo, one stay left open."""
    open_stay(db, datetime(2026, 8, 20, 6, 10, 46))

    def boom(*_args, **_kwargs):
        raise RuntimeError("lock timeout")

    monkeypatch.setattr(
        parking_session_service, "reconcile_open_session_for_reentry", boom
    )

    _reconcile_stale_stays_on_reentry(db, PLATE, REENTRY)  # must not raise


def test_an_aware_reentry_time_is_compared_in_facility_local(db):
    """event_time can arrive tz-aware; the DB columns are naive facility-local."""
    from app.config import facility_tz

    stale = open_stay(db, datetime(2026, 8, 20, 6, 10, 46))

    _reconcile_stale_stays_on_reentry(
        db, PLATE, REENTRY.replace(tzinfo=facility_tz())
    )

    db.refresh(stale)
    assert stale.status == "closed"
    assert stale.exit_time == REENTRY
