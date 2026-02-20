# tests/test_entry_exit_service.py
"""Unit tests for the entry/exit service (Phase 2 — UC1 + UC2 + UC4)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta
from app.services.entry_exit_service import handle_anpr_event
from app.services.event_parser import ParsedCameraEvent


def make_anpr_event(plate="ABC-1234", gate="entry"):
    return ParsedCameraEvent(
        camera_id=f"CAM-{'ENTRY' if gate == 'entry' else 'EXIT'}",
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id=gate,
        channel_name="Test ANPR",
        trigger_time=datetime.utcnow(),
        raw_xml="{}",
        plate_number=plate,
        gate=gate,
    )


class TestEntryExitService:
    @pytest.mark.asyncio
    async def test_entry_event_creates_log(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None  # no vehicle

        with patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock):
            await handle_anpr_event(make_anpr_event(), db)

        db.add.assert_called_once()
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_no_plate_skipped(self):
        event = make_anpr_event(plate=None)
        db = MagicMock()

        with patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_anpr_event(event, db)
            db.add.assert_not_called()
            mock_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_vehicle_triggers_alert(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None  # unknown

        with patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_anpr_event(make_anpr_event(), db)
            mock_alert.assert_called_once()
            assert "Unregistered" in mock_alert.call_args[1].get("description", mock_alert.call_args[0][-1])
