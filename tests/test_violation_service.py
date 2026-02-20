# tests/test_violation_service.py
"""Unit tests for the violation service (UC5)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from app.services.violation_service import handle_violation_event
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
