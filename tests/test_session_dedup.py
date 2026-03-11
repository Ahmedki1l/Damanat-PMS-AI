# tests/test_session_dedup.py
"""Unit tests for session-based event deduplication (Issue #22)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta

from app.services.event_parser import ParsedCameraEvent
from app.services.violation_service import handle_violation_event, _active_sessions as violation_sessions
from app.services.intrusion_service import handle_intrusion_event, _active_sessions as intrusion_sessions


def _make_violation_event():
    return ParsedCameraEvent(
        camera_id="CAM-04",
        device_serial="TEST",
        channel_id=1,
        event_type="fielddetection",
        detection_target="vehicle",
        region_id="restricted-vip",
        channel_name="Test",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
    )


def _make_intrusion_event():
    return ParsedCameraEvent(
        camera_id="CAM-14",
        device_serial="TEST",
        channel_id=1,
        event_type="fielddetection",
        detection_target="vehicle",
        region_id="emergency-exit",
        channel_name="Test",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
    )


class TestViolationSessionDedup:
    @pytest.fixture(autouse=True)
    def clear_sessions(self):
        violation_sessions.clear()
        yield
        violation_sessions.clear()

    @pytest.mark.asyncio
    async def test_rapid_events_only_one_alert(self):
        """10 rapid events from same camera+zone → only 1 DB insert + 1 alert."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with patch("app.services.violation_service.create_alert", new_callable=AsyncMock) as mock_alert:
            for _ in range(10):
                await handle_violation_event(_make_violation_event(), db)

            mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_reset_after_max_duration(self):
        """Session exceeding MAX_DURATION → resets, next event treated as new entry."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with (
            patch("app.services.violation_service.create_alert", new_callable=AsyncMock) as mock_alert,
            patch("app.services.violation_service.settings") as mock_settings,
        ):
            mock_settings.EVENT_STREAM_SUPPRESS_SECONDS = 30
            mock_settings.EVENT_STREAM_MAX_DURATION_SECONDS = 300
            mock_settings.VIOLATION_COOLDOWN_SECONDS = 30
            mock_settings.RESTRICTED_ZONES = "restricted-vip"
            mock_settings.ALWAYS_VIOLATION_EVENTS = "linedetection"

            # First event — starts session
            await handle_violation_event(_make_violation_event(), db)
            assert mock_alert.call_count == 1

            # Simulate session expiry by backdating the stored timestamp
            key = ("CAM-04", "restricted-vip")
            violation_sessions[key] = datetime.utcnow() - timedelta(seconds=301)

            # Next event after expiry — should reset and create a new alert
            await handle_violation_event(_make_violation_event(), db)
            assert mock_alert.call_count == 2


class TestIntrusionSessionDedup:
    @pytest.fixture(autouse=True)
    def clear_sessions(self):
        intrusion_sessions.clear()
        yield
        intrusion_sessions.clear()

    @pytest.mark.asyncio
    async def test_rapid_events_only_one_alert(self):
        """10 rapid events from same camera+zone → only 1 DB insert + 1 alert."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with (
            patch("app.services.intrusion_service.create_alert", new_callable=AsyncMock) as mock_alert,
            patch("app.services.intrusion_service.resolve_zone", return_value="emergency-exit"),
        ):
            for _ in range(10):
                await handle_intrusion_event(_make_intrusion_event(), db)

            mock_alert.assert_called_once()

    @pytest.mark.asyncio
    async def test_session_reset_after_max_duration(self):
        """Session exceeding MAX_DURATION → resets, next event treated as new entry."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with (
            patch("app.services.intrusion_service.create_alert", new_callable=AsyncMock) as mock_alert,
            patch("app.services.intrusion_service.resolve_zone", return_value="emergency-exit"),
            patch("app.services.intrusion_service.settings") as mock_settings,
        ):
            mock_settings.EVENT_STREAM_SUPPRESS_SECONDS = 30
            mock_settings.EVENT_STREAM_MAX_DURATION_SECONDS = 300
            mock_settings.INTRUSION_COOLDOWN_SECONDS = 30

            # First event — starts session
            await handle_intrusion_event(_make_intrusion_event(), db)
            assert mock_alert.call_count == 1

            # Simulate session expiry
            key = ("CAM-14", "emergency-exit")
            intrusion_sessions[key] = datetime.utcnow() - timedelta(seconds=301)

            # Next event after expiry — should create new alert
            await handle_intrusion_event(_make_intrusion_event(), db)
            assert mock_alert.call_count == 2
