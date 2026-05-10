# tests/test_parking_stats.py
import sys
import os
import pytest
from datetime import datetime, timedelta, UTC
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base
from app.models.entry_exit_log import EntryExitLog
from app.services.entry_exit_service import handle_anpr_event
from app.services.event_parser import ParsedCameraEvent
from app.models.entry_exit_log import EntryExitLog as _EntryExitLogModel
from app.config import facility_tz
from app.routers.parking_stats import get_avg_parking_time, get_daily_stats

# Setup in-memory SQLite for testing
engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def make_anpr_event(cam_id, plate, gate, timestamp=None):
    return ParsedCameraEvent(
        camera_id=cam_id,
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id="1",
        channel_name="Gate",
        trigger_time=timestamp or datetime.now(UTC),
        raw_xml="<test/>",
        crossing_direction="B-to-A",
        plate_number=plate,
        gate=gate
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

@pytest.mark.asyncio
async def test_parking_duration_calculation(db):
    """Verify that exit event matches entry and calculates duration."""
    plate = "TEST-123"
    # Naive facility-local timestamps (DB convention)
    entry_time_naive = (datetime.now(UTC) - timedelta(minutes=30)).astimezone(facility_tz()).replace(tzinfo=None)
    exit_time = datetime.now(UTC)

    # Seed the entry log directly — this test covers duration/stats logic,
    # not the two-phase ANPR detection flow (covered by test_entry_exit_service).
    db.add(_EntryExitLogModel(
        plate_number=plate,
        vehicle_id=None,
        vehicle_type="unknown",
        gate="entry",
        camera_id="CAM-ENTRY",
        event_time=entry_time_naive,
        created_at=entry_time_naive,
    ))
    db.commit()

    # Exit Event — drives duration calculation and matching
    exit_event = make_anpr_event("CAM-EXIT", plate, "exit", exit_time)
    with patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock):
        with patch("app.services.entry_exit_service.parking_session_service.close_session"):
            await handle_anpr_event(exit_event, db)
    db.commit()
    
    # Verify records
    logs = db.query(EntryExitLog).order_by(EntryExitLog.id).all()
    assert len(logs) == 2
    
    entry_log = logs[0]
    exit_log = logs[1]
    
    assert exit_log.matched_entry_id == entry_log.id
    assert entry_log.matched_entry_id == exit_log.id
    assert exit_log.parking_duration == 1800 # 30 mins
    
    # 3. Verify Stats Endpoint logic
    target_dt = entry_time_naive.strftime("%Y-%m-%d")
    stats = get_avg_parking_time(target_date=target_dt, db=db)
    assert stats["avg_parking_minutes"] == 30.0
    
    daily = get_daily_stats(target_date=target_dt, db=db)
    assert daily["total_vehicles"] == 1
    assert daily["avg_parking_minutes"] == 30.0

@pytest.mark.asyncio
async def test_unmatched_exit_handles_null(db):
    """If no entry exists, duration should be None."""
    plate = "NO-ENTRY"
    exit_event = make_anpr_event("CAM-EXIT", plate, "exit")
    
    with patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock):
        await handle_anpr_event(exit_event, db)
    db.commit()
    
    exit_log = db.query(EntryExitLog).filter(EntryExitLog.plate_number == plate).first()
    assert exit_log.parking_duration is None
    assert exit_log.matched_entry_id is None

