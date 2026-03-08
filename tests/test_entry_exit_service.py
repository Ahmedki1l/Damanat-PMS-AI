# tests/test_entry_exit_service.py
"""Unit tests for the entry/exit service (UC1 + UC2 + UC4)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from datetime import datetime, timedelta
from app.services.entry_exit_service import handle_anpr_event
from app.services.event_parser import ParsedCameraEvent
from app.models.entry_exit_log import EntryExitLog


def make_anpr_event(plate="ABC-1234", gate="entry", trigger_time=None):
    return ParsedCameraEvent(
        camera_id=f"CAM-{'ENTRY' if gate == 'entry' else 'EXIT'}",
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id=gate,
        channel_name="Test ANPR",
        trigger_time=trigger_time or datetime.utcnow(),
        raw_xml="{}",
        plate_number=plate,
        gate=gate,
    )


class TestEntryExitService:
    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_creates_log(self, mock_vs, mock_alert):
        """Entry event should create a log record."""
        mock_vs.lookup_vehicle.return_value = None  # unknown vehicle
        db = MagicMock()

        await handle_anpr_event(make_anpr_event(), db)

        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "ABC-1234"
        assert log.gate == "entry"
        assert log.vehicle_type == "unknown"

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_no_plate_skipped(self, mock_vs, mock_alert):
        """ANPR event with no plate should be silently skipped."""
        db = MagicMock()
        await handle_anpr_event(make_anpr_event(plate=None), db)
        db.add.assert_not_called()
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_known_vehicle_no_alert(self, mock_vs, mock_alert):
        """Known vehicle should NOT trigger unknown_vehicle alert."""
        vehicle = MagicMock()
        vehicle.id = 1
        vehicle.vehicle_type = "employee"
        mock_vs.lookup_vehicle.return_value = vehicle
        db = MagicMock()

        await handle_anpr_event(make_anpr_event(), db)

        mock_alert.assert_not_called()
        log = db.add.call_args[0][0]
        assert log.vehicle_id == 1
        assert log.vehicle_type == "employee"

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unknown_vehicle_triggers_alert(self, mock_vs, mock_alert):
        """Unknown vehicle should trigger unknown_vehicle alert."""
        mock_vs.lookup_vehicle.return_value = None
        db = MagicMock()

        await handle_anpr_event(make_anpr_event(), db)

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args[1]
        assert kwargs["alert_type"] == "unknown_vehicle"
        assert "ABC-1234" in kwargs["description"]

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_exit_matches_entry(self, mock_vs, mock_alert):
        """Exit event should match the most recent unmatched entry and calculate duration."""
        mock_vs.lookup_vehicle.return_value = None

        entry_time = datetime(2026, 3, 8, 9, 0, 0)
        exit_time = datetime(2026, 3, 8, 11, 30, 0)  # 2.5 hours later

        # Simulate the matching entry record (no spec — allows attribute assignment)
        mock_entry = MagicMock()
        mock_entry.id = 42
        mock_entry.event_time = entry_time
        mock_entry.matched_entry_id = None

        db = MagicMock()
        # The query chain: db.query().filter().filter().order_by().first()
        # MagicMock auto-chains, so set the terminal .first() on the deepest mock
        query_chain = db.query.return_value
        for _ in range(10):  # cover any depth of .filter/.order_by chaining
            query_chain.filter.return_value = query_chain
            query_chain.order_by.return_value = query_chain
        query_chain.first.return_value = mock_entry

        await handle_anpr_event(make_anpr_event(gate="exit", trigger_time=exit_time), db)

        log = db.add.call_args[0][0]
        assert log.matched_entry_id == 42
        assert log.parking_duration == 9000  # 2.5 hours = 9000 seconds

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_exit_no_matching_entry(self, mock_vs, mock_alert):
        """Exit with no matching entry should still create log but with no duration."""
        mock_vs.lookup_vehicle.return_value = None
        db = MagicMock()
        query_chain = db.query.return_value
        for _ in range(10):
            query_chain.filter.return_value = query_chain
            query_chain.order_by.return_value = query_chain
        query_chain.first.return_value = None

        await handle_anpr_event(make_anpr_event(gate="exit"), db)

        log = db.add.call_args[0][0]
        assert log.parking_duration is None
        assert log.matched_entry_id is None

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_vehicle_service_called_with_plate(self, mock_vs, mock_alert):
        """Should use vehicle_service.lookup_vehicle (repository pattern), not direct query."""
        mock_vs.lookup_vehicle.return_value = None
        db = MagicMock()

        await handle_anpr_event(make_anpr_event(plate="XYZ-999"), db)

        mock_vs.lookup_vehicle.assert_called_once_with(db, "XYZ-999")
