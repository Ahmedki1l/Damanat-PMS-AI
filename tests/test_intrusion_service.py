# tests/test_intrusion_service.py
"""Unit tests for the intrusion service (UC6)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models.alert import Alert
from app.services.intrusion_service import handle_intrusion_event
from app.services.event_parser import ParsedCameraEvent
from app.config import settings


def make_event(region_id: str):
    return ParsedCameraEvent(
        camera_id="CAM-07",
        device_serial="TEST",
        channel_id=1,
        event_type="fielddetection",
        detection_target="vehicle",
        region_id=region_id,
        channel_name="Test",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
    )


# Setup in-memory SQLite for testing
engine = create_engine("sqlite:///:memory:")
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db_session():
    Base.metadata.create_all(bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.mark.asyncio
async def test_two_regions_create_distinct_alerts(db_session, monkeypatch):
    monkeypatch.setattr(
        settings,
        "CAMERA_REGION_ZONE_MAP",
        "CAM-07:0=emergency-exit;CAM-07:1=staff-only-area",
    )

    await handle_intrusion_event(make_event("0"), db_session)
    await handle_intrusion_event(make_event("1"), db_session)
    db_session.commit()

    alerts = db_session.query(Alert).order_by(Alert.id).all()
    assert len(alerts) == 2

    # zone_id and zone_name must both be set and match the resolved zone
    assert {a.zone_id for a in alerts} == {"emergency-exit", "staff-only-area"}
    assert {a.region_id for a in alerts} == {0, 1}

    # zone_name column (real DB field) must equal zone_id for each row
    for a in alerts:
        assert a.zone_name is not None, f"Alert {a.id} has NULL zone_name"
        assert a.zone_name == a.zone_id, (
            f"Alert {a.id}: zone_name={a.zone_name!r} != zone_id={a.zone_id!r}"
        )