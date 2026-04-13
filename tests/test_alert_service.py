"""Unit tests for alert enrichment logic."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch

import pytest

from app.services.alert_service import create_alert


@pytest.mark.asyncio
@patch("app.services.alert_service.event_bus")
async def test_create_alert_persists_severity_and_location(mock_event_bus):
    db = MagicMock()

    await create_alert(
        db=db,
        alert_type="unknown_vehicle",
        camera_id="CAM-ENTRY",
        zone_id="entry",
        zone_name="Entry Gate",
        event_type="AccessControllerEvent",
        description="Unknown vehicle arrived",
        plate_number="ABC-1234",
        snapshot_path="evidence.jpg",
    )

    db.add.assert_called_once()
    alert = db.add.call_args[0][0]
    assert alert.plate_number == "ABC-1234"
    assert alert.severity == "critical"
    assert alert.location_display == "Entry Gate"
    assert alert.snapshot_path == "evidence.jpg"
    mock_event_bus.publish.assert_called_once()

