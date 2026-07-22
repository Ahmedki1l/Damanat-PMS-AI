"""Transaction-level simulations for session-derived occupancy aggregates.

Line crossings are wake-up signals only.  The aggregate rows must always be
replaced from open ``parking_sessions``; direction and duplicate line events
must never add/subtract a counter that can drift over time.
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
    async def test_entry_transition_and_exit_follow_session_state(self, db):
        """Each line only republishes the current open-session projection."""
        seed_zones(db, total=5, b1=3, b2=2)
        for index in range(3):
            add_open_session(db, f"B1-{index}", "B1")
        for index in range(2):
            add_open_session(db, f"B2-{index}", "B2")
        db.commit()

        entering = add_open_session(db, "JOURNEY-1", "B1")
        await process_and_commit(make_event("CAM-03"), db)
        assert counts(db) == (6, 4, 2)

        entering.floor = "B2"
        entering.updated_at = datetime.now(UTC).replace(tzinfo=None)
        await process_and_commit(make_event("CAM-09"), db)
        assert counts(db) == (6, 3, 3)

        entering.status = "closed"
        entering.exit_time = datetime.now(UTC).replace(tzinfo=None)
        entering.updated_at = entering.exit_time
        await process_and_commit(make_event("CAM-08"), db)
        assert counts(db) == (5, 3, 2)
        assert count(db, TOTAL) == count(db, B1) + count(db, B2)

    @pytest.mark.asyncio
    async def test_direction_cannot_change_counts_without_session_change(self, db):
        seed_zones(db, total=99, b1=99, b2=99)
        add_open_session(db, "STABLE-1", "B1")
        add_open_session(db, "STABLE-2", "B2")
        db.commit()

        await process_and_commit(make_event("CAM-03", region_id="1"), db)
        assert counts(db) == (2, 1, 1)

        _processed_events_cache.clear()
        await process_and_commit(make_event("CAM-03", region_id="2"), db)
        assert counts(db) == (2, 1, 1)


class TestDedupAndTransactions:
    @pytest.mark.asyncio
    async def test_duplicate_line_event_is_dropped_without_applying_a_delta(self, db):
        seed_zones(db, total=50, b1=50, b2=50)
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
