# tests/test_entry_exit_service.py
"""Unit tests for the entry/exit service (Phase 2 — UC1 + UC2 + UC4)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, UTC, timedelta
from app.models.vehicle import Vehicle
from app.services.entry_exit_service import (
    handle_anpr_event,
    confirm_pending_entry,
    _pending_entries,
)
from app.services.event_parser import ParsedCameraEvent

def make_anpr_event(plate="ABC-1234", gate="entry", trigger_time=None):
    """Helper to create a mocked ANPR event."""
    return ParsedCameraEvent(
        camera_id=f"CAM-{'ENTRY' if gate == 'entry' else 'EXIT'}",
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id=gate,
        channel_name="Test ANPR",
        trigger_time=trigger_time or datetime.now(UTC),
        raw_xml="{}",
        plate_number=plate,
        gate=gate,
    )

class TestEntryExitService:
    def setup_method(self):
        _pending_entries.clear()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_event_creates_log(self, mock_vs, mock_open_session, mock_alert):
        """UC1: ANPR entry writes log immediately when USE_CAM03_ENTRY_CONFIRMATION=False (default)."""
        placeholder_vehicle = MagicMock(spec=Vehicle)
        placeholder_vehicle.id = 42
        placeholder_vehicle.plate_number = "ABC-1234"
        placeholder_vehicle.vehicle_type = "unknown"
        placeholder_vehicle.owner_name = "Unknown"
        placeholder_vehicle.is_employee = False
        placeholder_vehicle.is_registered = False
        mock_vs.ensure_unregistered_vehicle.return_value = placeholder_vehicle
        db = MagicMock()
        query_chain = db.query.return_value
        query_chain.filter.return_value = query_chain
        query_chain.order_by.return_value = query_chain
        query_chain.first.return_value = None  # no dedup hit, no anti-bounce hit

        await handle_anpr_event(make_anpr_event(), db)

        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "ABC-1234"
        assert log.gate == "entry"
        assert log.vehicle_id == 42
        assert log.vehicle_type == "unknown"
        mock_vs.ensure_unregistered_vehicle.assert_called_once_with(db, "ABC-1234")
        mock_open_session.assert_called_once()
        assert "ABC-1234" not in _pending_entries

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_two_phase_flow(self, mock_vs, mock_open_session, mock_alert, mock_settings):
        """UC1: With USE_CAM03_ENTRY_CONFIRMATION=True, entry defers until CAM-03 confirms."""
        mock_settings.USE_CAM03_ENTRY_CONFIRMATION = True
        mock_settings.CAMERAS = {"CAM-ENTRY": {"gate": "entry"}}
        mock_settings.ENTRY_ANTIBOUNCE_SECONDS = 30
        placeholder_vehicle = MagicMock(spec=Vehicle)
        placeholder_vehicle.id = 42
        placeholder_vehicle.plate_number = "ABC-1234"
        placeholder_vehicle.vehicle_type = "unknown"
        placeholder_vehicle.owner_name = "Unknown"
        placeholder_vehicle.is_employee = False
        placeholder_vehicle.is_registered = False
        mock_vs.ensure_unregistered_vehicle.return_value = placeholder_vehicle
        db = MagicMock()
        query_chain = db.query.return_value
        query_chain.filter.return_value = query_chain
        query_chain.order_by.return_value = query_chain
        query_chain.first.return_value = None

        # Phase 1: ANPR fires — no DB write yet, plate sits in pending cache
        await handle_anpr_event(make_anpr_event(), db)

        db.add.assert_not_called()
        assert "ABC-1234" in _pending_entries

        # Phase 2: CAM-03 confirms entry — log is written to DB
        await confirm_pending_entry(db)

        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "ABC-1234"
        assert log.gate == "entry"
        assert log.vehicle_id == 42
        mock_open_session.assert_called_once()
        assert "ABC-1234" not in _pending_entries

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_no_plate_skipped(self, mock_vs, mock_alert):
        """ANPR events without a plate number should be ignored."""
        db = MagicMock()
        await handle_anpr_event(make_anpr_event(plate=None), db)
        
        db.add.assert_not_called()
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unknown_vehicle_triggers_alert(self, mock_vs, mock_alert):
        """UC4: Unregistered vehicle at entry must trigger an alert (fires before CAM-03 confirm)."""
        placeholder_vehicle = MagicMock(spec=Vehicle)
        placeholder_vehicle.id = 42
        placeholder_vehicle.plate_number = "ABC-1234"
        placeholder_vehicle.vehicle_type = "unknown"
        placeholder_vehicle.owner_name = "Unknown"
        placeholder_vehicle.is_employee = False
        placeholder_vehicle.is_registered = False
        mock_vs.ensure_unregistered_vehicle.return_value = placeholder_vehicle
        db = MagicMock()
        query_chain = db.query.return_value
        query_chain.filter.return_value = query_chain
        query_chain.order_by.return_value = query_chain
        query_chain.first.return_value = None  # no dedup, no anti-bounce

        await handle_anpr_event(make_anpr_event(), db)

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args[1]
        assert kwargs["alert_type"] == "unknown_vehicle"
        assert "ABC-1234" in kwargs["description"]

    @pytest.mark.asyncio
    @patch("app.services.alert_service.broadcast_event", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_existing_unregistered_vehicle_still_triggers_alert(self, mock_vs, mock_alert, mock_close_session, mock_broadcast):
        vehicle = MagicMock(spec=Vehicle)
        vehicle.id = 7
        vehicle.plate_number = "ABC-1234"
        vehicle.vehicle_type = "unknown"
        vehicle.owner_name = "Unknown"
        vehicle.is_employee = False
        vehicle.is_registered = False
        mock_vs.ensure_unregistered_vehicle.return_value = vehicle

        db = MagicMock()
        query_chain = db.query.return_value
        query_chain.filter.return_value = query_chain
        query_chain.order_by.return_value = query_chain
        query_chain.first.return_value = None  # no dedup, no matching entry

        await handle_anpr_event(make_anpr_event(gate="exit"), db)

        mock_alert.assert_called_once()
        mock_broadcast.assert_not_called()

    # ── UC2: Duration calculation ─────────────────────────────────────────
    
    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_exit_calculates_parking_duration(self, mock_vs, mock_close_session, mock_alert):
        """UC2: Duration should be calculated correctly in seconds when matching an entry."""
        mock_vs.lookup_vehicle.return_value = None
        
        entry_time = datetime(2026, 3, 8, 9, 0, 0)
        exit_time = datetime(2026, 3, 8, 11, 0, 0)  # 2 hours later
        
        # Mocking the database query to find the previous entry
        matching_entry = MagicMock()
        matching_entry.id = 101
        matching_entry.event_time = entry_time
        
        db = MagicMock()
        # SQLAlchemy chain: db.query().filter().filter().order_by().first()
        query_chain = db.query.return_value
        query_chain.filter.return_value = query_chain
        query_chain.order_by.return_value = query_chain
        query_chain.first.side_effect = [None, matching_entry]

        await handle_anpr_event(make_anpr_event(gate="exit", trigger_time=exit_time), db)

        log = db.add.call_args[0][0]
        assert log.matched_entry_id == 101
        assert log.parking_duration == 7200  # 2 hours = 7200 seconds
        mock_close_session.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unmatched_exit_has_null_duration(self, mock_vs, mock_close_session, mock_alert):
        """Exit events with no prior entry record should have null duration."""
        mock_vs.lookup_vehicle.return_value = None
        db = MagicMock()
        
        query_chain = db.query.return_value
        query_chain.filter.return_value = query_chain
        query_chain.order_by.return_value = query_chain
        query_chain.first.side_effect = [None, None] # dedup miss, then no entry found

        await handle_anpr_event(make_anpr_event(gate="exit"), db)

        log = db.add.call_args[0][0]
        assert log.parking_duration is None
        assert log.matched_entry_id is None
        mock_close_session.assert_called_once()


