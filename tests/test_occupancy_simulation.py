"""Transaction-level simulations for the occupancy aggregates.

Three rows, two sources:

  GARAGE-TOTAL = COUNT(open parking_sessions)      — authoritative, cannot drift
  B2-PARKING   = running delta from the ramp cams  — the only counted aggregate
  B1-PARKING   = GARAGE-TOTAL - B2                 — derived, never counted

B2 cannot be session-derived: ``parking_sessions.floor`` is only written when
VA binds the car to a slot, so between the gate and the bind a car has
floor=NULL and no per-floor query can see it. The ramp cameras see it cross
immediately, which is why B2 is counted from crossings instead.

That makes B2 the one number that can drift, so the drift is BOUNDED rather
than banned: it is clamped to [0, max_capacity] and healed down to
GARAGE-TOTAL whenever it exceeds it — an empty garage therefore always ends
with an empty B2. A crossing from any non-ramp camera is still a wake-up
signal only.
"""

import os
import sys
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.config import settings
from app.database import Base
from app.models.parking_session import ParkingSession
from app.models.zone_occupancy import ZoneOccupancy
from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import (
    _processed_events_cache,
    handle_occupancy_event,
    record_event_in_cache,
)


TOTAL = settings.GARAGE_TOTAL_ZONE
B1 = settings.B1_PARKING_ZONE
B2 = settings.B2_PARKING_ZONE


# SQLite needs explicit transaction control for the service's nested savepoint.
engine = create_engine("sqlite:///:memory:", echo=False)


@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    del connection_record
    dbapi_conn.isolation_level = None


@sa_event.listens_for(engine, "begin")
def _do_begin(conn):
    conn.exec_driver_sql("BEGIN")


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
def clear_cache():
    _processed_events_cache.clear()
    yield
    _processed_events_cache.clear()


def seed_zones(db, *, total=0, b1=0, b2=0, capacity=100):
    now = datetime.now(UTC).replace(tzinfo=None)
    for zone_id, count, camera_id in (
        (TOTAL, total, "CAM-03"),
        (B1, b1, "CAM-03"),
        (B2, b2, "CAM-09"),
    ):
        metadata = settings.get_zone_metadata(zone_id)
        db.add(
            ZoneOccupancy(
                zone_id=zone_id,
                zone_name=metadata.get("zone_name"),
                floor=metadata.get("floor"),
                camera_id=camera_id,
                current_count=count,
                max_capacity=capacity,
                last_updated=now,
            )
        )
    db.commit()


def add_open_session(db, plate: str, floor: str) -> ParkingSession:
    now = datetime.now(UTC).replace(tzinfo=None)
    session = ParkingSession(
        plate_number=plate,
        entry_time=now,
        entry_camera_id="CAM-ENTRY",
        floor=floor,
        status="open",
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.flush()
    return session


def make_event(
    camera_id: str,
    *,
    region_id: str = "1",
) -> ParsedCameraEvent:
    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial="TEST-SN",
        channel_id=1,
        event_type="linedetection",
        detection_target="vehicle",
        region_id=region_id,
        channel_name=f"Test {camera_id}",
        trigger_time=datetime.now(UTC),
        raw_xml="<test/>",
    )


async def process_and_commit(event, db):
    with patch(
        "app.services.occupancy_service.create_alert",
        new_callable=AsyncMock,
    ):
        cache_key = await handle_occupancy_event(event, db)
    db.commit()
    if cache_key is not None:
        record_event_in_cache(cache_key)
    return cache_key


def count(db, zone_id: str) -> int:
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).one()
    db.refresh(zone)
    return int(zone.current_count)


def counts(db) -> tuple[int, int, int]:
    return count(db, TOTAL), count(db, B1), count(db, B2)


class TestSessionDerivedJourney:
    @pytest.mark.asyncio
    async def test_one_cars_full_journey_through_both_floors(self, db):
        """Entry → down to B2 → back up to B1 → exit, one car at a time.

        This is the model in full: the total follows sessions, B2 follows the
        ramp, and B1 is whatever is in the garage but not on B2.
        """
        seed_zones(db, total=0, b1=0, b2=0)

        # Enters the garage. Lands on B1 by definition — nothing has taken it
        # down the ramp yet.
        add_open_session(db, "JOURNEY-1", None)
        await process_and_commit(make_event("CAM-03"), db)
        assert counts(db) == (1, 1, 0)

        # Drives down the ramp past CAM-09's entrance-facing line.
        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-09", region_id="1"), db)
        assert counts(db) == (1, 0, 1)

        # Comes back up past the exit-facing line.
        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-09", region_id="2"), db)
        assert counts(db) == (1, 1, 0)

        # Leaves the garage.
        session = db.query(ParkingSession).filter_by(plate_number="JOURNEY-1").one()
        session.status = "closed"
        session.exit_time = datetime.now(UTC).replace(tzinfo=None)
        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-08"), db)
        assert counts(db) == (0, 0, 0)

    @pytest.mark.asyncio
    async def test_the_floors_always_add_up_to_the_total(self, db):
        """B1 + B2 == GARAGE-TOTAL is an invariant, not a coincidence.

        It holds because B1 is derived as TOTAL - B2 rather than counted
        separately — two independent counters could not guarantee it.
        """
        seed_zones(db, total=0, b1=0, b2=0)
        for index in range(5):
            add_open_session(db, f"CAR-{index}", None)
        db.commit()

        for region in ("1", "1", "2", "1"):
            _processed_events_cache.clear()
            await process_and_commit(make_event("CAM-09", region_id=region), db)
            total, b1, b2 = counts(db)
            assert b1 + b2 == total, f"floors {b1}+{b2} != total {total}"

        assert counts(db) == (5, 3, 2)

    @pytest.mark.asyncio
    async def test_a_non_ramp_camera_never_moves_b2(self, db):
        """CAM-03/CAM-08 crossings stay wake-up signals only, in either
        direction. Only the configured ramp cameras push a delta."""
        seed_zones(db, total=0, b1=0, b2=0)
        add_open_session(db, "STABLE-1", None)
        add_open_session(db, "STABLE-2", None)
        db.commit()

        await process_and_commit(make_event("CAM-03", region_id="1"), db)
        assert counts(db) == (2, 2, 0)

        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-03", region_id="2"), db)
        assert counts(db) == (2, 2, 0)


class TestB2CrossingDeltas:
    @pytest.mark.asyncio
    async def test_both_ramp_cameras_count_independently(self, db):
        """CAM-09 and CAM-10 cover separate passages, so a car crosses exactly
        one of them and BOTH cameras' crossings count. Nothing is deduped
        across cameras — see Settings.B2_CROSSING_CAMERAS."""
        seed_zones(db, total=0, b1=0, b2=0)
        for index in range(4):
            add_open_session(db, f"RAMP-{index}", None)
        db.commit()

        await process_and_commit(make_event("CAM-09", region_id="1"), db)
        await process_and_commit(make_event("CAM-10", region_id="1"), db)
        assert counts(db) == (4, 2, 2)

    @pytest.mark.asyncio
    async def test_two_cars_down_the_same_ramp_both_count(self, db):
        """The regression the 30s dedup window would have caused.

        Two cars past the same camera and line, seconds apart, are two cars.
        Only a repeat inside OCCUPANCY_CROSSING_DEDUP_SECONDS — one physical
        crossing that Hikvision fired twice — may be dropped.
        """
        seed_zones(db, total=0, b1=0, b2=0)
        for index in range(2):
            add_open_session(db, f"CONVOY-{index}", None)
        db.commit()

        await process_and_commit(make_event("CAM-09", region_id="1"), db)
        assert count(db, B2) == 1

        # Same camera + line again, but the immediate repeat is the duplicate
        # of a single crossing and must NOT count.
        assert await process_and_commit(make_event("CAM-09", region_id="1"), db) is None
        assert count(db, B2) == 1

        # Once the short window has passed it is a second car, and it counts.
        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-09", region_id="1"), db)
        assert counts(db) == (2, 0, 2)

    @pytest.mark.asyncio
    async def test_b2_never_goes_negative(self, db):
        """An exit-facing crossing with nothing on B2 clamps at zero rather
        than parking a negative count in the dashboard forever."""
        seed_zones(db, total=0, b1=0, b2=0)
        add_open_session(db, "PHANTOM-1", None)
        db.commit()

        await process_and_commit(make_event("CAM-09", region_id="2"), db)
        assert counts(db) == (1, 1, 0)

    @pytest.mark.asyncio
    async def test_b2_heals_down_when_it_exceeds_open_sessions(self, db):
        """A missed exit crossing leaves B2 too high. Open sessions are
        authoritative for how many cars exist at all, so B2 is healed down to
        the total instead of drifting until someone resets the zone by hand."""
        seed_zones(db, total=0, b1=0, b2=7)
        add_open_session(db, "REAL-1", None)
        db.commit()

        await process_and_commit(make_event("CAM-03"), db)
        assert counts(db) == (1, 0, 1)


class TestDedupAndTransactions:
    @pytest.mark.asyncio
    async def test_duplicate_line_event_is_dropped_without_applying_a_delta(self, db):
        seed_zones(db, total=50, b1=50, b2=0)
        add_open_session(db, "DUP-1", "B1")
        db.commit()

        first = await process_and_commit(make_event("CAM-03"), db)
        second = await process_and_commit(make_event("CAM-03"), db)

        assert first == ("CAM-03", "linedetection", "1")
        assert second is None
        assert counts(db) == (1, 1, 0)

    @pytest.mark.asyncio
    async def test_savepoint_rolls_back_partial_reconciliation(self, db):
        seed_zones(db, total=10, b1=7, b2=3)

        def fail_after_one_write(db_session, *, camera_id):
            del camera_id
            total = (
                db_session.query(ZoneOccupancy)
                .filter(ZoneOccupancy.zone_id == TOTAL)
                .one()
            )
            total.current_count = 0
            db_session.flush()
            raise RuntimeError("simulated failure during aggregate reconciliation")

        with patch(
            "app.services.occupancy_service.reconcile_zone_counts_from_open_sessions",
            side_effect=fail_after_one_write,
        ), patch(
            "app.services.occupancy_service.create_alert",
            new_callable=AsyncMock,
        ):
            cache_key = await handle_occupancy_event(make_event("CAM-03"), db)
        db.commit()

        assert cache_key is None
        assert counts(db) == (10, 7, 3)

    @pytest.mark.asyncio
    async def test_retry_reconciles_after_outer_transaction_rollback(self, db):
        seed_zones(db, total=5, b1=5, b2=0)
        for index in range(6):
            add_open_session(db, f"ROLLBACK-{index}", "B1")
        db.commit()
        cache_key = ("CAM-03", "linedetection", "1")

        with patch(
            "app.services.occupancy_service.create_alert",
            new_callable=AsyncMock,
        ):
            returned = await handle_occupancy_event(make_event("CAM-03"), db)

        assert returned == cache_key
        assert cache_key not in _processed_events_cache
        assert counts(db) == (6, 6, 0)

        db.rollback()
        assert counts(db) == (5, 5, 0)
        assert cache_key not in _processed_events_cache

        await process_and_commit(make_event("CAM-03"), db)
        assert counts(db) == (6, 6, 0)
        assert cache_key in _processed_events_cache


class TestNoNegativeCounts:
    @pytest.mark.asyncio
    async def test_exit_wakeup_on_empty_sessions_sets_every_count_to_zero(self, db):
        seed_zones(db, total=1, b1=1, b2=1)

        await process_and_commit(make_event("CAM-08"), db)
        assert counts(db) == (0, 0, 0)

        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-08"), db)
        assert counts(db) == (0, 0, 0)
