# tests/test_occupancy_service.py
"""Unit tests for the occupancy service (UC3) — linedetection + region_id direction."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from app.services.occupancy_service import handle_occupancy_event
from app.services.event_parser import ParsedCameraEvent

def make_event(region_id="1", event_type="linedetection"):
    """
    Helper: creates an event from CAM-04.
    By default: region_id="1" → entrance, region_id="2" → exit.
    """
    return ParsedCameraEvent(
        camera_id="CAM-04",
        device_serial="TEST-SN",
        channel_id=1,
        event_type=event_type,
        detection_target="vehicle",
        region_id=region_id,
        channel_name="CAM-04 (B1-PARKING)",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
    )

class TestOccupancyService:

    @pytest.mark.asyncio
    async def test_new_zone_created_on_first_event(self):
        """UC3: Zone row is auto-created if it doesn't exist in the DB."""
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("1"), db)

        db.add.assert_called_once()
        # Updated: Service performs a flush to prepare data; Dispatcher handles the commit.
        db.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_entrance_increments_count(self):
        """Line 1 crossing (entrance) increments the zone count."""
        zone = MagicMock()
        zone.current_count = 3
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("1"), db)

        assert zone.current_count == 4

    @pytest.mark.asyncio
    async def test_exit_decrements_count(self):
        """Line 2 crossing (exit) decrements the zone count."""
        zone = MagicMock()
        zone.current_count = 5
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("2"), db)

        assert zone.current_count == 4

    @pytest.mark.asyncio
    async def test_count_never_goes_negative(self):
        """Count should floor at 0 even if exit events are received."""
        zone = MagicMock()
        zone.current_count = 0
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("2"), db)

        assert zone.current_count == 0

    @pytest.mark.asyncio
    async def test_alert_triggered_at_90_percent(self):
        """UC3: Alert fires when occupancy reaches or exceeds 90%."""
        zone = MagicMock()
        zone.current_count = 8   # Will become 9 after entrance
        zone.max_capacity = 10   # 9/10 = 90% -> Alert trigger
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_occupancy_event(make_event("1"), db)
            
            mock_alert.assert_called_once()
            # Verify alert type in either positional or keyword arguments
            args, kwargs = mock_alert.call_args
            alert_type = kwargs.get("alert_type") or args[1]
            assert alert_type == "occupancy_full"

    @pytest.mark.asyncio
    async def test_alert_not_triggered_below_threshold(self):
        """No alert should be sent if occupancy is below 90%."""
        zone = MagicMock()
        zone.current_count = 5
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock) as mock_alert:
            await handle_occupancy_event(make_event("1"), db)
            mock_alert.assert_not_called()