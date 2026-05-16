# tests/test_occupancy_service.py
"""Unit tests for the occupancy service (UC3) — linedetection + region_id direction.

Updated to use real SQLite DB instead of MagicMock, since the service now uses
savepoints (begin_nested) and atomic SQL updates (func.max) which require a
real DB session.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, UTC
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.zone_occupancy import ZoneOccupancy
from app.services.occupancy_service import (
    handle_occupancy_event,
    record_event_in_cache,
    _processed_events_cache,
)
from app.services.event_parser import ParsedCameraEvent
from app.config import settings

# SQLite SAVEPOINT support (required for begin_nested in occupancy service)
engine = create_engine("sqlite:///:memory:")

@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
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


def seed_zones(db, total=0, b1=0, b2=0, capacity=10):
    for zone_id, count, cam in [
        (settings.GARAGE_TOTAL_ZONE, total, "CAM-03"),
        (settings.B1_PARKING_ZONE, b1, "CAM-03"),
        (settings.B2_PARKING_ZONE, b2, "CAM-09"),
    ]:
        db.add(ZoneOccupancy(
            zone_id=zone_id, camera_id=cam,
            current_count=count, max_capacity=capacity,
            last_updated=datetime.now(UTC),
        ))
    db.commit()


def get_count(db, zone_id):
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if zone:
        db.refresh(zone)
        return zone.current_count
    return -1


def make_event(region_id="1", cam_id="CAM-03", event_type="linedetection"):
    """
    Helper: creates an event from an occupancy camera.
    Uses CAM-03 (main entrance) by default.
    region_id="1" → entrance, region_id="2" → exit.
    """
    return ParsedCameraEvent(
        camera_id=cam_id,
        device_serial="TEST-SN",
        channel_id=1,
        event_type=event_type,
        detection_target="vehicle",
        region_id=region_id,
        channel_name=f"{cam_id} test",
        trigger_time=datetime.now(UTC),
        raw_xml="<test/>",
    )


class TestOccupancyService:

    @pytest.mark.asyncio
    async def test_new_zone_auto_created_on_first_event(self, db):
        """UC3: Zone rows are auto-created if they don't exist in the DB."""
        # Start with empty DB (no zones seeded)
        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            cache_key = await handle_occupancy_event(make_event("1"), db)
        db.commit()

        total = db.query(ZoneOccupancy).filter(
            ZoneOccupancy.zone_id == settings.GARAGE_TOTAL_ZONE
        ).first()
        assert total is not None, "GARAGE-TOTAL should be auto-created"
        assert total.current_count == 1
        assert total.zone_name == settings.get_zone_metadata(settings.GARAGE_TOTAL_ZONE)["zone_name"]
        assert total.floor == settings.get_zone_metadata(settings.GARAGE_TOTAL_ZONE)["floor"]

    @pytest.mark.asyncio
    async def test_entrance_increments_count(self, db):
        """Line 1 crossing (entrance via CAM-03) increments TOTAL and B1."""
        seed_zones(db, total=3, b1=3, b2=0)

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("1", "CAM-03"), db)
        db.commit()

        assert get_count(db, settings.GARAGE_TOTAL_ZONE) == 4
        assert get_count(db, settings.B1_PARKING_ZONE) == 4

    @pytest.mark.asyncio
    async def test_exit_decrements_count(self, db):
        """Line 1 crossing on exit camera (CAM-08) decrements TOTAL and B1."""
        seed_zones(db, total=5, b1=5, b2=0)

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("1", "CAM-08"), db)
        db.commit()

        assert get_count(db, settings.GARAGE_TOTAL_ZONE) == 4
        assert get_count(db, settings.B1_PARKING_ZONE) == 4

    @pytest.mark.asyncio
    async def test_count_never_goes_negative(self, db):
        """Count should floor at 0 even if exit events are received on empty zone."""
        seed_zones(db, total=0, b1=0, b2=0)

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("1", "CAM-08"), db)
        db.commit()

        assert get_count(db, settings.GARAGE_TOTAL_ZONE) == 0
        assert get_count(db, settings.B1_PARKING_ZONE) == 0

    @pytest.mark.asyncio
    async def test_alert_not_triggered_at_exact_capacity(self, db):
        """UC3: At exactly 100% occupancy (=full) the alert does NOT fire.
        `capacity_exceeded` is reserved for strict overflow (>100%) — full is
        a normal operating state, not an exception."""
        seed_zones(db, total=9, b1=9, b2=0, capacity=10)

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock) as mock_alert, \
             patch("app.services.occupancy_service._real_capacity_state",
                   new_callable=AsyncMock, return_value=(10, 10)):
            await handle_occupancy_event(make_event("1", "CAM-03"), db)
        db.commit()

        # Real-source check says 10 parked / 10 slots = full but not exceeded.
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_triggered_when_exceeded(self, db):
        """UC3: Alert fires when real parked vehicles strictly exceed slots."""
        seed_zones(db, total=10, b1=10, b2=0, capacity=10)

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock) as mock_alert, \
             patch("app.services.occupancy_service._real_capacity_state",
                   new_callable=AsyncMock, return_value=(10, 11)):
            await handle_occupancy_event(make_event("1", "CAM-03"), db)
        db.commit()

        assert mock_alert.call_count >= 1
        alert_types = [
            call.kwargs.get("alert_type", call.args[1] if len(call.args) > 1 else None)
            for call in mock_alert.call_args_list
        ]
        assert "capacity_exceeded" in alert_types
        # Description should carry the real numbers, not line-crossing.
        descriptions = [
            call.kwargs.get("description", "")
            for call in mock_alert.call_args_list
            if call.kwargs.get("alert_type") == "capacity_exceeded"
        ]
        assert any("11 parked / 10 slots" in d for d in descriptions), (
            f"description should include real counts; got {descriptions!r}"
        )

    @pytest.mark.asyncio
    async def test_alert_not_triggered_below_threshold(self, db):
        """No alert when real occupancy is comfortably below capacity."""
        seed_zones(db, total=5, b1=5, b2=0, capacity=10)

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock) as mock_alert, \
             patch("app.services.occupancy_service._real_capacity_state",
                   new_callable=AsyncMock, return_value=(10, 5)):
            await handle_occupancy_event(make_event("1", "CAM-03"), db)
        db.commit()

        mock_alert.assert_not_called()


