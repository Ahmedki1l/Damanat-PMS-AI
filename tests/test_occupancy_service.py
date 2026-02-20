# tests/test_occupancy_service.py
"""Unit tests for the occupancy service (UC3)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime
from app.services.occupancy_service import handle_occupancy_event
from app.services.event_parser import ParsedCameraEvent


def make_event(event_type="regionEntrance", region_id="parking-row-A"):
    return ParsedCameraEvent(
        camera_id="CAM-03",
        device_serial="TEST",
        channel_id=1,
        event_type=event_type,
        detection_target="vehicle",
        region_id=region_id,
        channel_name="Test",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
    )


class TestOccupancyService:
    @pytest.mark.asyncio
    async def test_new_zone_created_on_first_event(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event(), db)

        db.add.assert_called_once()
        db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_entrance_increments_count(self):
        zone = MagicMock()
        zone.current_count = 3
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("regionEntrance"), db)

        assert zone.current_count == 4

    @pytest.mark.asyncio
    async def test_exit_decrements_count(self):
        zone = MagicMock()
        zone.current_count = 5
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("regionExiting"), db)

        assert zone.current_count == 4

    @pytest.mark.asyncio
    async def test_count_never_goes_negative(self):
        zone = MagicMock()
        zone.current_count = 0
        zone.max_capacity = 10
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = zone

        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            await handle_occupancy_event(make_event("regionExiting"), db)

        assert zone.current_count == 0
