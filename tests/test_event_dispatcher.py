# tests/test_event_dispatcher.py
"""Routing tests for event_dispatcher — entry-ramp confirmation wiring (UC1)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from datetime import datetime, UTC
from unittest.mock import MagicMock, AsyncMock, patch

import app.services.event_dispatcher as dispatcher
from app.services.event_dispatcher import dispatch_event
from app.services.event_parser import ParsedCameraEvent


def make_anpr_event(cam_id, plate, event_type="ANPR"):
    return ParsedCameraEvent(
        camera_id=cam_id,
        device_serial="x",
        channel_id=1,
        event_type=event_type,
        detection_target="vehicle",
        region_id=None,
        channel_name="lpr",
        trigger_time=datetime.now(UTC),
        raw_xml="{}",
        snapshot_path="snap.jpg",          # set → skips the ISAPI snapshot fetch
        local_snapshot_path="snap.jpg",
        plate_number=plate,
    )


def make_line_event(cam_id, direction="forward", region_id="1"):
    return ParsedCameraEvent(
        camera_id=cam_id,
        device_serial="x",
        channel_id=1,
        event_type="linedetection",
        detection_target="vehicle",
        region_id=region_id,
        channel_name="ramp",
        trigger_time=datetime.now(UTC),
        raw_xml="<x/>",
        snapshot_path="snap.jpg",          # set → skips the ISAPI snapshot fetch
        local_snapshot_path="snap.jpg",
        crossing_direction=direction,
        plate_number=None,
    )


def configure(mock_settings, *, confirm_cams, cameras):
    mock_settings.ENTRY_CONFIRM_CAMERAS = confirm_cams
    mock_settings.CAMERAS = cameras
    mock_settings.CAM23_ENTRY_LINE = ""
    mock_settings.CAM23_ENTRY_DIRECTION = ""
    mock_settings.LOG_CAMERA_FILTER = ""
    mock_settings.LOG_CAMERA_EXCLUDE = ""


@pytest.mark.asyncio
@patch("app.services.alert_service.broadcast_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.add_event_to_feed")
@patch("app.services.entry_exit_service.confirm_entry_crossing", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.settings")
async def test_configured_ramp_cam_confirms(mock_settings, mock_confirm, mock_feed, mock_broadcast):
    """A line-crossing from a camera listed in ENTRY_CONFIRM_CAMERAS (and not an
    occupancy cam) confirms the entry burst, tagged with its own camera id."""
    configure(mock_settings, confirm_cams="CAM-99,CAM-03", cameras={"CAM-99": {}})

    await dispatch_event(make_line_event("CAM-99"), MagicMock())

    mock_confirm.assert_awaited_once()
    assert mock_confirm.await_args.kwargs["source_cam"] == "CAM-99"


@pytest.mark.asyncio
@patch("app.services.alert_service.broadcast_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.add_event_to_feed")
@patch("app.services.entry_exit_service.confirm_entry_crossing", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.settings")
async def test_unconfigured_cam_does_not_confirm(mock_settings, mock_confirm, mock_feed, mock_broadcast):
    """A line-crossing from a camera NOT in ENTRY_CONFIRM_CAMERAS is ignored."""
    configure(mock_settings, confirm_cams="CAM-23,CAM-03", cameras={"CAM-50": {}})

    await dispatch_event(make_line_event("CAM-50"), MagicMock())

    mock_confirm.assert_not_awaited()


@pytest.mark.asyncio
@patch("app.services.alert_service.broadcast_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.handle_occupancy_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.add_event_to_feed")
@patch("app.services.entry_exit_service.confirm_entry_crossing", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.settings")
async def test_occupancy_confirm_cam_not_double_confirmed(mock_settings, mock_confirm, mock_feed, mock_occ, mock_broadcast):
    """An occupancy cam in ENTRY_CONFIRM_CAMERAS (CAM-03) confirms via
    occupancy_service, so the dispatcher must NOT also route it here."""
    configure(mock_settings, confirm_cams="CAM-23,CAM-03", cameras={"CAM-03": {"gate": "entry"}})
    mock_occ.return_value = None

    await dispatch_event(make_line_event("CAM-03"), MagicMock())

    mock_occ.assert_awaited_once()        # handled by the occupancy path
    mock_confirm.assert_not_awaited()     # not double-confirmed here


def _configure_anpr(mock_settings, cameras):
    mock_settings.CAMERAS = cameras
    mock_settings.ANPR_BURST_MAX_SECONDS = 20.0
    mock_settings.LOG_CAMERA_FILTER = ""
    mock_settings.LOG_CAMERA_EXCLUDE = ""
    mock_settings.ENTRY_CONFIRM_CAMERAS = ""


@pytest.mark.asyncio
@patch("app.services.event_dispatcher.handle_anpr_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.add_event_to_feed")
@patch("app.services.event_dispatcher.settings")
async def test_unknown_anpr_rescued_by_prior_vmr(mock_settings, mock_feed, mock_anpr):
    """A vehicleMatchResult resolves the entry camera; the follow-on ANPR arrives
    UNKNOWN (gateway NAT). The ANPR must inherit CAM-ENTRY so it isn't dropped."""
    dispatcher._VMR_GATE_HINTS.clear()
    _configure_anpr(mock_settings, {"CAM-ENTRY": {"gate": "entry"}})

    # 1) entry LPR fires the imageless match result — resolves to CAM-ENTRY
    await dispatch_event(make_anpr_event("CAM-ENTRY", "DJS-7842", "vehicleMatchResult"), MagicMock())
    mock_anpr.assert_not_awaited()        # VMR itself is not processed

    # 2) follow-on ANPR arrives with an unresolvable identity
    await dispatch_event(make_anpr_event("UNKNOWN-10.1.20.60", "DJS-7842"), MagicMock())

    mock_anpr.assert_awaited_once()
    rescued = mock_anpr.await_args.args[0]
    assert rescued.camera_id == "CAM-ENTRY"
    assert rescued.gate == "entry"


@pytest.mark.asyncio
@patch("app.services.event_dispatcher.handle_anpr_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.add_event_to_feed")
@patch("app.services.event_dispatcher.settings")
async def test_unknown_anpr_without_vmr_not_rescued(mock_settings, mock_feed, mock_anpr):
    """No prior vehicleMatchResult for the plate → the UNKNOWN ANPR is left as-is
    (handle_anpr_event will drop it for 'no gate'), never misattributed."""
    dispatcher._VMR_GATE_HINTS.clear()
    _configure_anpr(mock_settings, {"CAM-ENTRY": {"gate": "entry"}})

    await dispatch_event(make_anpr_event("UNKNOWN-10.1.20.60", "GHOST-0001"), MagicMock())

    # still dispatched (handler decides), but identity untouched
    rescued = mock_anpr.await_args.args[0]
    assert rescued.camera_id == "UNKNOWN-10.1.20.60"
    assert rescued.gate is None


@pytest.mark.asyncio
@patch("app.services.event_dispatcher.handle_anpr_event", new_callable=AsyncMock)
@patch("app.services.event_dispatcher.add_event_to_feed")
@patch("app.services.event_dispatcher.settings")
async def test_resolved_anpr_not_overridden(mock_settings, mock_feed, mock_anpr):
    """An ANPR that already resolved (CAM-EXIT) is never rewritten, even if a VMR
    hint exists for the same plate."""
    dispatcher._VMR_GATE_HINTS.clear()
    _configure_anpr(mock_settings, {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}})

    await dispatch_event(make_anpr_event("CAM-ENTRY", "SHR-5918", "vehicleMatchResult"), MagicMock())
    await dispatch_event(make_anpr_event("CAM-EXIT", "SHR-5918"), MagicMock())

    rescued = mock_anpr.await_args.args[0]
    assert rescued.camera_id == "CAM-EXIT"    # untouched — was already resolved
