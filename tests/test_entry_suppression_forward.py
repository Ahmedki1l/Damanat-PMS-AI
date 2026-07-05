# tests/test_entry_suppression_forward.py
"""B1 — decouple the VA identity forward from PMS occupancy suppression.

dedup and anti-bounce must suppress ONLY the PMS-side occupancy record (the
EntryExitLog row, the open session, the UC4 alert). They must NOT suppress the
forward of the identity image to VA (port 8000): VA has its own dedup and needs
the image to (re)build the car's ReID identity + on-disk folder. This is the
root cause of DJS-7842 getting no folder — its anti-bounce-suppressed entry
never reached VA.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, timedelta

from app.services.entry_exit_service import (
    handle_anpr_event,
    flush_due_entry_bursts,
    _entry_bursts,
    _pending_crossings,
    _recent_entries,
)
from app.services.event_parser import ParsedCameraEvent
from app.config import facility_now_naive


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_anpr_event(plate="ABC-1234", trigger_time=None, pic_num=1):
    return ParsedCameraEvent(
        camera_id="CAM-ENTRY",
        device_serial="TEST-ANPR",
        channel_id=1,
        event_type="AccessControllerEvent",
        detection_target="vehicle",
        region_id="entry",
        channel_name="Test ANPR",
        trigger_time=trigger_time,
        raw_xml="{}",
        plate_number=plate,
        gate="entry",
        pic_num=pic_num,
        plate_confidence=90,
    )


def make_db(first_side_effect=None):
    db = MagicMock()
    q = db.query.return_value
    q.filter.return_value = q
    q.order_by.return_value = q
    if first_side_effect is not None:
        q.first.side_effect = first_side_effect
    else:
        q.first.return_value = None
    return db


def configure_settings(mock_settings, *, antibounce: int):
    mock_settings.USE_CAM03_ENTRY_CONFIRMATION = False  # idle window alone flushes
    mock_settings.CAMERAS = {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}}
    mock_settings.ENTRY_ANTIBOUNCE_SECONDS = antibounce
    mock_settings.ANPR_BURST_WINDOW_SECONDS = 2.5
    mock_settings.ANPR_BURST_MAX_SECONDS = 8.0
    mock_settings.ENTRY_CONFIRM_DIRECTIONS = "CAM-23:ramp-entry,CAM-03:B-entry"
    mock_settings.ENTRY_CONFIRM_MATCH_SECONDS = 30.0


def _force_idle():
    old = facility_now_naive() - timedelta(seconds=999)
    for b in _entry_bursts.values():
        b["last_read_at"] = old


def _recent_exit_row(event_time):
    row = MagicMock()
    row.event_time = event_time
    return row


class TestSuppressionStillForwards:
    def setup_method(self):
        _entry_bursts.clear()
        _pending_crossings.clear()
        _recent_entries.clear()

    # ── Anti-bounce: DB suppressed, VA still notified ──────────────────────
    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_antibounce_suppresses_db_but_forwards_to_va(
        self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms
    ):
        configure_settings(mock_settings, antibounce=30)
        t = datetime(2026, 3, 8, 12, 0, 0)  # naive facility-local
        db = make_db(first_side_effect=[
            None,                                   # dedup: no recent entry
            _recent_exit_row(t - timedelta(seconds=10)),  # anti-bounce: exit 10s ago
        ])

        await handle_anpr_event(make_anpr_event(plate="DJS-7842", trigger_time=t), db)
        _force_idle()
        await flush_due_entry_bursts(db)

        # Occupancy suppressed: no EntryExitLog row, no session, no vehicle row.
        db.add.assert_not_called()
        mock_open.assert_not_called()
        mock_vs.ensure_unregistered_vehicle.assert_not_called()

        # But VA WAS notified with the entry identity image.
        entry_pushes = [c for c in mock_pms.await_args_list if c.args[1] == "entry"]
        assert len(entry_pushes) == 1
        assert entry_pushes[0].args[0] == "DJS-7842"

    # ── Dedup: DB suppressed, VA still notified ────────────────────────────
    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_dedup_suppresses_db_but_forwards_to_va(
        self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms
    ):
        configure_settings(mock_settings, antibounce=0)
        t = datetime(2026, 3, 8, 12, 0, 0)
        db = make_db(first_side_effect=[
            _recent_exit_row(t - timedelta(seconds=5)),  # dedup: a recent entry exists
            None,
        ])

        await handle_anpr_event(make_anpr_event(plate="LLJ-9005", trigger_time=t), db)
        _force_idle()
        await flush_due_entry_bursts(db)

        db.add.assert_not_called()
        mock_open.assert_not_called()
        mock_vs.ensure_unregistered_vehicle.assert_not_called()

        entry_pushes = [c for c in mock_pms.await_args_list if c.args[1] == "entry"]
        assert len(entry_pushes) == 1
        assert entry_pushes[0].args[0] == "LLJ-9005"

    # ── Control: a normal entry still writes the DB AND forwards ───────────
    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_normal_entry_still_writes_and_forwards(
        self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms
    ):
        configure_settings(mock_settings, antibounce=30)
        v = MagicMock()
        v.id = 42
        v.vehicle_type = "unknown"
        v.is_registered = False
        mock_vs.ensure_unregistered_vehicle.return_value = v
        t = datetime(2026, 3, 8, 12, 0, 0)
        db = make_db(first_side_effect=[None, None])  # no dedup, no recent exit

        await handle_anpr_event(make_anpr_event(plate="OKAY-0001", trigger_time=t), db)
        _force_idle()
        await flush_due_entry_bursts(db)

        # Occupancy recorded.
        db.add.assert_called_once()
        assert db.add.call_args[0][0].plate_number == "OKAY-0001"
        mock_open.assert_called_once()
        # And VA notified.
        entry_pushes = [c for c in mock_pms.await_args_list if c.args[1] == "entry"]
        assert len(entry_pushes) == 1
        assert entry_pushes[0].args[0] == "OKAY-0001"
