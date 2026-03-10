# tests/test_violation_service.py
"""Unit tests for the violation service (UC5)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.database import Base
from app.models.alert import Alert
from app.config import settings
from app.services.violation_service import handle_violation_event, resolve_violation_on_exit
from app.services.event_parser import ParsedCameraEvent


def make_event(event_type="fielddetection", region_id="restricted-vip"):
    return ParsedCameraEvent(
        camera_id="CAM-01",
        device_serial="TEST",
        channel_id=1,
        event_type=event_type,
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


class TestViolationService:
    @pytest.mark.asyncio
    async def test_restricted_zone_triggers_alert(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None  # no recent

        with patch("app.services.violation_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_violation_event(make_event(), db)
            mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_restricted_zone_ignored(self):
        db = MagicMock()

        with patch("app.services.violation_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_violation_event(make_event(region_id="regular-parking"), db)
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_linedetection_always_triggers(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with patch("app.services.violation_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_violation_event(make_event("linedetection", "any-zone"), db)
            mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_cooldown_suppresses_duplicate(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock()  # recent exists

        with patch("app.services.violation_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_violation_event(make_event(), db)
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_resolves_and_reentry_respects_cooldown(self, db_session):
        await handle_violation_event(make_event(), db_session)
        db_session.commit()

        alert = db_session.query(Alert).filter(Alert.alert_type == "violation").first()
        assert alert is not None
        assert alert.is_resolved is False

        await resolve_violation_on_exit("CAM-01", "restricted-vip", db_session)
        db_session.refresh(alert)
        assert alert.is_resolved is True
        assert alert.resolved_at is not None

        await handle_violation_event(make_event(), db_session)
        db_session.commit()
        assert db_session.query(Alert).count() == 1

        alert.triggered_at = datetime.utcnow() - timedelta(seconds=settings.VIOLATION_COOLDOWN_SECONDS + 1)
        db_session.commit()

        await handle_violation_event(make_event(), db_session)
        db_session.commit()
        assert db_session.query(Alert).count() == 2
        open_alerts = db_session.query(Alert).filter(Alert.is_resolved == False).all()  # noqa: E712
        assert len(open_alerts) == 1