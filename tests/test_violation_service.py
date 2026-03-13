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
from app.services.violation_service import handle_violation_event, resolve_violation_on_exit, clear_session
import app.services.violation_service as _vs
from app.services.event_parser import ParsedCameraEvent


def make_event(event_type="fielddetection", region_id="restricted-vip", camera_id="CAM-01"):
    return ParsedCameraEvent(
        camera_id=camera_id,
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
    _vs._active_sessions.clear()   # reset in-memory session state between tests
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
        clear_session("CAM-01", "restricted-vip")  # expire in-memory session so entry is treated fresh

        await handle_violation_event(make_event(), db_session)
        db_session.commit()
        assert db_session.query(Alert).count() == 2
        open_alerts = db_session.query(Alert).filter(Alert.is_resolved == False).all()  # noqa: E712
        assert len(open_alerts) == 1

    @pytest.mark.asyncio
    async def test_zone_name_stored_as_column(self, db_session):
        """zone_name must be saved as a real DB column, not just a property."""
        await handle_violation_event(make_event(), db_session)
        db_session.commit()

        alert = db_session.query(Alert).filter(Alert.alert_type == "violation").first()
        assert alert is not None
        # zone_name column must be populated
        assert alert.zone_name is not None
        # For CAM-01 with region_id="restricted-vip" (no ZONE_MAPPING hit) the
        # canonical zone falls back to the raw region_id string itself.
        assert alert.zone_name == alert.zone_id

    @pytest.mark.asyncio
    async def test_two_regions_same_camera_two_distinct_alerts(self, db_session, monkeypatch):
        """
        Two events from the same camera with different regionIDs must produce two
        separate alerts, each with the correct zone_name and region_id.
        CAM-02: zone1 → restricted-vip, zone2 → no-parking-zone
        """
        monkeypatch.setattr(
            settings,
            "CAMERA_REGION_ZONE_MAP",
            "CAM-02:1=restricted-vip;CAM-02:2=no-parking-zone",
        )

        # Event from region 1 (restricted-vip) — should trigger
        await handle_violation_event(make_event(region_id="1", camera_id="CAM-02"), db_session)
        db_session.commit()

        # Clear the in-memory dedup session so region 2 event isn't deduped
        _vs._active_sessions.clear()

        # Event from region 2 (no-parking-zone) — also in RESTRICTED_ZONES, should trigger
        # We patch RESTRICTED_ZONES to include no-parking-zone for this test
        import app.services.violation_service as vs_mod
        original = vs_mod._get_restricted_zones
        vs_mod._get_restricted_zones = lambda: {"restricted-vip", "no-parking-zone"}
        try:
            await handle_violation_event(make_event(region_id="2", camera_id="CAM-02"), db_session)
            db_session.commit()
        finally:
            vs_mod._get_restricted_zones = original

        alerts = db_session.query(Alert).order_by(Alert.id).all()
        assert len(alerts) == 2, f"Expected 2 alerts, got {len(alerts)}"

        zone_names = {a.zone_name for a in alerts}
        zone_ids = {a.zone_id for a in alerts}
        region_ids = {a.region_id for a in alerts}

        assert "restricted-vip" in zone_names
        assert "no-parking-zone" in zone_names
        assert zone_ids == zone_names  # zone_id mirrors zone_name
        assert region_ids == {1, 2}