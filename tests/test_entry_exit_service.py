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
    _cam03_pre_confirmations,
    CAM03_PRE_CONFIRM_TTL_SECONDS,
    PENDING_ENTRY_TTL_SECONDS,
    _cleanup_pending,
)
from app.services.event_parser import ParsedCameraEvent
from app.config import facility_now_naive


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_anpr_event(plate="ABC-1234", gate="entry", trigger_time=None):
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


def make_vehicle(registered=False):
    v = MagicMock(spec=Vehicle)
    v.id = 42
    v.plate_number = "ABC-1234"
    v.vehicle_type = "unknown"
    v.owner_name = "Unknown"
    v.is_employee = False
    v.is_registered = registered
    return v


def make_db(first_side_effect=None):
    """Mock DB session. first_side_effect: list of values for sequential .first() calls."""
    db = MagicMock()
    q = db.query.return_value
    q.filter.return_value = q
    q.order_by.return_value = q
    if first_side_effect:
        q.first.side_effect = first_side_effect
    else:
        q.first.return_value = None
    return db


def configure_settings(mock_settings, *, two_phase: bool):
    """Configure a patched settings mock for entry/exit tests."""
    mock_settings.USE_CAM03_ENTRY_CONFIRMATION = two_phase
    mock_settings.CAMERAS = {
        "CAM-ENTRY": {"gate": "entry"},
        "CAM-EXIT":  {"gate": "exit"},
    }
    mock_settings.ENTRY_ANTIBOUNCE_SECONDS = 0  # disabled — keeps DB queries simple


# ── Test class ────────────────────────────────────────────────────────────────

class TestEntryExitService:
    def setup_method(self):
        _pending_entries.clear()
        _cam03_pre_confirmations.clear()
        # Stub out the vehicle-repo lookup so it never touches the mock DB session.
        # All tests treat the plate as "new" (None → vehicle_newly_created=True) by
        # default; override per-test where needed.
        self._vr_patch = patch(
            "app.repositories.vehicle_repository.vehicle_repo.get_by_plate",
            return_value=None,
        )
        self._vr_patch.start()

    def teardown_method(self):
        self._vr_patch.stop()

    # ── UC1: Two-phase disabled — immediate write ──────────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_immediate_write(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """USE_CAM03_ENTRY_CONFIRMATION=False: ANPR entry writes to DB immediately."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(), db)

        # Entry written straight away — no deferral, no tokens
        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "ABC-1234"
        assert log.gate == "entry"
        assert log.vehicle_id == 42
        mock_open.assert_called_once()
        assert "ABC-1234" not in _pending_entries
        assert len(_cam03_pre_confirmations) == 0

    # ── UC1: Two-phase — ANPR arrives before CAM-03 ───────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_anpr_first_then_cam03(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """Two-phase: ANPR arrives first → deferred; CAM-03 fires → entry written to DB."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        # Step 1: ANPR fires — no pre-confirmation, goes to pending
        await handle_anpr_event(make_anpr_event(), db)
        db.add.assert_not_called()
        assert "ABC-1234" in _pending_entries
        assert len(_cam03_pre_confirmations) == 0

        # Step 2: CAM-03 confirms — consumes pending, writes entry
        await confirm_pending_entry(db)
        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "ABC-1234"
        assert log.gate == "entry"
        assert log.vehicle_id == 42
        mock_open.assert_called_once()
        assert "ABC-1234" not in _pending_entries
        assert len(_cam03_pre_confirmations) == 0  # no leftover token

    # ── UC1: Two-phase — CAM-03 fires before ANPR (real-world common case) ─

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_cam03_fires_first(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """Two-phase: CAM-03 fires first (hardware line detection is faster than ANPR
        plate recognition). Pre-confirmation stored; ANPR writes immediately on arrival."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        # Step 1: CAM-03 fires — no pending ANPR yet, pre-confirmation stored
        await confirm_pending_entry(db)
        db.add.assert_not_called()
        assert len(_pending_entries) == 0
        assert len(_cam03_pre_confirmations) == 1

        # Step 2: ANPR arrives — consumes pre-confirmation, writes immediately
        await handle_anpr_event(make_anpr_event(), db)
        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "ABC-1234"
        assert log.gate == "entry"
        assert log.vehicle_id == 42
        mock_open.assert_called_once()
        assert len(_cam03_pre_confirmations) == 0  # token consumed

    # ── UC1: Expired pre-confirmation must not be consumed ────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_cam03_preconfirm_expires(self, mock_vs, mock_alert, mock_settings, mock_pms):
        """A pre-confirmation older than CAM03_PRE_CONFIRM_TTL_SECONDS is discarded;
        the ANPR event must defer to pending rather than writing immediately."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        # Inject an already-expired token directly
        expired_ts = facility_now_naive() - timedelta(seconds=CAM03_PRE_CONFIRM_TTL_SECONDS + 1)
        _cam03_pre_confirmations.append(expired_ts)

        await handle_anpr_event(make_anpr_event(), db)

        # Expired token must be discarded — entry deferred to pending
        db.add.assert_not_called()
        assert "ABC-1234" in _pending_entries
        assert len(_cam03_pre_confirmations) == 0  # cleaned up

    # ── Guard: no plate ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_no_plate_skipped(self, mock_vs, mock_alert):
        """ANPR events without a plate number are silently ignored."""
        await handle_anpr_event(make_anpr_event(plate=None), MagicMock())
        mock_alert.assert_not_called()

    # ── UC4: Alert on unregistered vehicle ────────────────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unregistered_entry_triggers_alert(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """UC4: Unregistered vehicle at entry gate triggers alert regardless of two-phase mode."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle(registered=False)

        await handle_anpr_event(make_anpr_event(), make_db())

        mock_alert.assert_called_once()
        kwargs = mock_alert.call_args[1]
        assert kwargs["alert_type"] == "unknown_vehicle"
        assert "ABC-1234" in kwargs["description"]

    @pytest.mark.asyncio
    @patch("app.services.alert_service.broadcast_event", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unregistered_exit_triggers_alert(self, mock_vs, mock_close, mock_alert, mock_settings, mock_pms, mock_broadcast):
        """UC4: Unregistered vehicle at exit gate triggers alert, not broadcast."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle(registered=False)

        await handle_anpr_event(make_anpr_event(gate="exit"), make_db())

        mock_alert.assert_called_once()
        mock_broadcast.assert_not_called()

    # ── UC2: Parking duration ─────────────────────────────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_exit_calculates_parking_duration(self, mock_vs, mock_close, mock_alert, mock_settings, mock_pms):
        """UC2: Parking duration is calculated correctly from the matching entry."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()

        entry_time = datetime(2026, 3, 8, 9, 0, 0)
        exit_time  = datetime(2026, 3, 8, 11, 0, 0)  # 2 hours later

        matching_entry = MagicMock()
        matching_entry.id = 101
        matching_entry.event_time = entry_time
        matching_entry.tzinfo = None

        # Query order: dedup check → matching entry lookup
        db = make_db(first_side_effect=[None, matching_entry])

        await handle_anpr_event(make_anpr_event(gate="exit", trigger_time=exit_time), db)

        log = db.add.call_args[0][0]
        assert log.matched_entry_id == 101
        assert log.parking_duration == 7200  # 2 hours = 7200 s
        mock_close.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unmatched_exit_has_null_duration(self, mock_vs, mock_close, mock_alert, mock_settings, mock_pms):
        """UC2: Exit with no prior entry record has null duration and no matched_entry_id."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()

        # Both dedup check and entry lookup return nothing
        db = make_db(first_side_effect=[None, None])

        await handle_anpr_event(make_anpr_event(gate="exit"), db)

        log = db.add.call_args[0][0]
        assert log.parking_duration is None
        assert log.matched_entry_id is None
        mock_close.assert_called_once()

    # ── Ghost vehicle cleanup on TTL expiry ───────────────────────────────

    def test_ghost_vehicle_deleted_on_expiry(self):
        """When CAM-03 never confirms within PENDING_ENTRY_TTL_SECONDS,
        _cleanup_pending should delete the freshly-created unregistered vehicle row."""
        from app.models.vehicle import Vehicle as VehicleModel

        expired_time = facility_now_naive() - timedelta(seconds=PENDING_ENTRY_TTL_SECONDS + 1)
        _pending_entries["ABC-1234"] = {
            "plate": "ABC-1234",
            "camera_id": "CAM-ENTRY",
            "event_time": expired_time,
            "snapshot_path": None,
            "vehicle_id": 42,
            "vehicle_type": "unknown",
            "is_employee": False,
            "vehicle_newly_created": True,
        }

        db = make_db()

        _cleanup_pending(db)

        db.query.assert_called_with(VehicleModel)
        q = db.query.return_value
        q.filter.assert_called_once()
        q.filter.return_value.delete.assert_called_once_with(synchronize_session=False)
        assert "ABC-1234" not in _pending_entries

    def test_no_ghost_deletion_for_existing_vehicle(self):
        """When the plate already existed before the ANPR event (vehicle_newly_created=False),
        _cleanup_pending must NOT delete the vehicles row."""
        expired_time = facility_now_naive() - timedelta(seconds=PENDING_ENTRY_TTL_SECONDS + 1)
        _pending_entries["XYZ-9999"] = {
            "plate": "XYZ-9999",
            "camera_id": "CAM-ENTRY",
            "event_time": expired_time,
            "snapshot_path": None,
            "vehicle_id": 99,
            "vehicle_type": "unknown",
            "is_employee": False,
            "vehicle_newly_created": False,
        }

        db = make_db()

        _cleanup_pending(db)

        db.query.assert_not_called()
        assert "XYZ-9999" not in _pending_entries
