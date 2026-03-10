# tests/test_multi_level_occupancy.py
import sys
import os
import pytest
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base
from app.models.zone_occupancy import ZoneOccupancy
from app.services.occupancy_service import handle_occupancy_event, process_pending_exits, _pending_exits, _processed_events_cache
from app.services.event_parser import ParsedCameraEvent
from app.config import settings

# Setup in-memory SQLite for testing
engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def make_event(cam_id, plate=None, direction="B-to-A", region_id="1"):
    return ParsedCameraEvent(
        camera_id=cam_id,
        device_serial="TEST-S/N",
        channel_id=1,
        event_type="ANPR" if "ENTRY" in cam_id or "EXIT" in cam_id else "linedetection",
        detection_target="vehicle",
        region_id=region_id,
        channel_name=f"Test {cam_id}",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
        crossing_direction=direction,
        plate_number=plate
    )

@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)

@pytest.fixture(autouse=True)
def reset_state():
    _pending_exits.clear()
    _processed_events_cache.clear()

def seed_zones(db):
    zones = [
        ZoneOccupancy(zone_id=settings.GARAGE_TOTAL_ZONE, current_count=10, max_capacity=100, camera_id="CAM-03"),
        ZoneOccupancy(zone_id=settings.B1_PARKING_ZONE, current_count=10, max_capacity=100, camera_id="CAM-03"),
        ZoneOccupancy(zone_id=settings.B2_PARKING_ZONE, current_count=5, max_capacity=100, camera_id="CAM-09")
    ]
    db.add_all(zones)
    db.commit()

@pytest.mark.asyncio
async def test_main_entry_increments_multiple_zones(db):
    """CAM-03 should increment both TOTAL and B1."""
    seed_zones(db)
    event = make_event("CAM-03", plate="ABC-123")
    
    with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
        await handle_occupancy_event(event, db)
    
    total = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.GARAGE_TOTAL_ZONE).first()
    b1 = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.B1_PARKING_ZONE).first()
    
    assert total.current_count == 11
    assert b1.current_count == 11

@pytest.mark.asyncio
async def test_b1_to_b2_transition(db):
    """CAM-09 should decrement B1 (pending) and increment B2 (immediate)."""
    seed_zones(db)
    event = make_event("CAM-09")
    
    with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
        await handle_occupancy_event(event, db)
    
    b1 = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.B1_PARKING_ZONE).first()
    b2 = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.B2_PARKING_ZONE).first()
    
    # B1 is pending exit -> stayed at seeded value (10)
    assert b1.current_count == 10
    # verify pending entry exists
    assert any(settings.B1_PARKING_ZONE in str(k) for k in _pending_exits.keys())
    
    # B2 is applied immediately -> 5 + 1 = 6
    assert b2.current_count == 6

@pytest.mark.asyncio
async def test_false_exit_prevention_direction(db):
    """Reverse crossing on exit camera (A-to-B) should be treated as re-entry (+1)."""
    seed_zones(db)
    # Role -1 (Exit) * Multiplier -1 (Reverse) = Delta +1
    # Now B-to-A is forward, so A-to-B is reverse
    event = make_event("CAM-08", direction="A-to-B", plate="REV-1")
    
    await handle_occupancy_event(event, db)
    
    total = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.GARAGE_TOTAL_ZONE).first()
    # Total 10 + 1 = 11
    assert total.current_count == 11

@pytest.mark.asyncio
async def test_false_exit_prevention_confirmation_window(db):
    """Exit should be pending until processed by background worker."""
    seed_zones(db)
    event = make_event("CAM-08", plate="TEST-PLATE")
    await handle_occupancy_event(event, db)
    
    assert len(_pending_exits) == 2
    
    total = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.GARAGE_TOTAL_ZONE).first()
    assert total.current_count == 10 # still 10 while pending
    
    # Hack timestamps to be 10s old
    for key in list(_pending_exits.keys()):
        zid, cid, delta, ts = _pending_exits[key]
        _pending_exits[key] = (zid, cid, delta, datetime.utcnow() - timedelta(seconds=10))
    
    await process_pending_exits(db)
    
    total = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.GARAGE_TOTAL_ZONE).first()
    # 10 - 1 = 9
    assert total.current_count == 9

@pytest.mark.asyncio
async def test_exit_cancelled_by_immediate_reentry(db):
    """If a car 'exits' but re-enters within window, it cancels the exit."""
    seed_zones(db)
    # 1. Forward exit (B-to-A) -> Pending
    exit_e = make_event("CAM-08", plate="STAY-IN", direction="B-to-A")
    await handle_occupancy_event(exit_e, db)
    assert len(_pending_exits) == 2
    
    # 2. Reverse re-entry (A-to-B) -> Cancellation
    _processed_events_cache.clear()
    reentry_e = make_event("CAM-08", plate="STAY-IN", direction="A-to-B")
    await handle_occupancy_event(reentry_e, db)
    
    # Pending exits should be cleaned up
    assert len(_pending_exits) == 0
    total = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == settings.GARAGE_TOTAL_ZONE).first()
    assert total.current_count == 10 # No change
