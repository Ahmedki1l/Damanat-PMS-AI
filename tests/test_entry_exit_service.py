# tests/test_entry_exit_service.py
"""Unit tests for the entry/exit service (Phase 2 — UC1 + UC2 + UC4).

Entry handling buffers the multi-read ANPR burst and writes ONE entry labeled
by the LAST (correct) read at flush time. These tests drive a burst, optionally
confirm it with a ramp crossing, force the debounce window to elapse, then call
the flusher and assert on the winning plate.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime, UTC, timedelta, timezone
from app.models.vehicle import Vehicle
from app.services.entry_exit_service import (
    handle_anpr_event,
    confirm_pending_entry,
    confirm_entry_crossing,
    flush_due_entry_bursts,
    drain_background_forwards,
    _entry_bursts,
    _pending_crossings,
    _recent_entries,
    CAM03_PMS_DIRECTION,
    SourceTimestampUnavailable,
)
from app.services.event_parser import ParsedCameraEvent
from app.config import facility_now_naive


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_anpr_event(plate="ABC-1234", gate="entry", trigger_time=None,
                    pic_num=None, plate_confidence=None):
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
        pic_num=pic_num,
        plate_confidence=plate_confidence,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "should_reject"),
    [("off", False), ("shadow", False), ("authoritative", True)],
)
async def test_exit_receive_time_fallback_is_mode_scoped(
    monkeypatch,
    mode,
    should_reject,
):
    monkeypatch.setattr(
        "app.services.entry_exit_service.settings.ENTRY_V2_MODE",
        mode,
    )
    event = make_anpr_event(gate="exit")
    event.trigger_time_source = "pms_receive_missing"
    db = MagicMock()

    if should_reject:
        with pytest.raises(SourceTimestampUnavailable):
            await handle_anpr_event(event, db)
        db.add.assert_not_called()
    else:
        result = await handle_anpr_event(event, db)
        assert result is not None

    if should_reject:
        db.add.assert_not_called()


def make_vehicle(registered=False, plate="ABC-1234"):
    v = MagicMock(spec=Vehicle)
    v.id = 42
    v.plate_number = plate
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
        results = iter(first_side_effect)
        q.first.side_effect = lambda: next(results, None)
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
    mock_settings.ANPR_BURST_WINDOW_SECONDS = 2.5
    mock_settings.ANPR_BURST_MAX_SECONDS = 8.0
    mock_settings.ENTRY_CONFIRM_DIRECTIONS = "CAM-23:ramp-entry,CAM-03:B-entry"
    mock_settings.ENTRY_CONFIRM_MATCH_SECONDS = 30.0


def _force_idle():
    """Push every open burst past its debounce window so the next flush commits it."""
    old = facility_now_naive() - timedelta(seconds=999)
    for b in _entry_bursts.values():
        b["last_read_at"] = old


def _age_pending_crossings():
    """Push every pending ramp crossing past its window so the flusher reaps it."""
    old = facility_now_naive() - timedelta(seconds=999)
    for c in _pending_crossings:
        c["ts"] = old


# ── Test class ────────────────────────────────────────────────────────────────

class TestEntryExitService:
    def setup_method(self):
        _entry_bursts.clear()
        _pending_crossings.clear()
        _recent_entries.clear()

    # ── UC1: the core fix — last read of the burst wins ────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_burst_labels_last_read(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """A burst with a WRONG early read and a RIGHT later read writes exactly
        ONE entry labeled by the RIGHT (last) read — and only the RIGHT plate is
        resolved as a vehicle and forwarded to the PMS."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle(plate="RIGHT-1234")
        db = make_db()

        # Burst: wrong read first (pic 1), correct read second (pic 2).
        await handle_anpr_event(make_anpr_event(plate="WRONG-0001", pic_num=1, plate_confidence=55), db)
        await handle_anpr_event(make_anpr_event(plate="RIGHT-1234", pic_num=2, plate_confidence=95), db)
        db.add.assert_not_called()  # nothing committed mid-burst

        # CAM-23 (ramp-top line next to the ANPR) only CONFIRMS the car — the
        # correct read lags the crossing, so nothing is written yet.
        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        db.add.assert_not_called()

        # Burst settles (lagging correct read is in) → background flusher writes.
        _force_idle()
        await flush_due_entry_bursts(db)

        # Exactly one entry row, labeled RIGHT, with the winning confidence.
        db.add.assert_called_once()
        log = db.add.call_args[0][0]
        assert log.plate_number == "RIGHT-1234"
        assert log.gate == "entry"
        assert log.plate_confidence == 95

        # Session opened for RIGHT only.
        mock_open.assert_called_once()
        assert mock_open.call_args.kwargs["plate_number"] == "RIGHT-1234"

        # Vehicle resolved only for the winning plate (no WRONG vehicle row).
        mock_vs.ensure_unregistered_vehicle.assert_called_once()
        assert mock_vs.ensure_unregistered_vehicle.call_args[0][1] == "RIGHT-1234"

        # PMS forwarding: one "entry" push, with RIGHT; WRONG never forwarded.
        entry_pushes = [c for c in mock_pms.await_args_list if c.args[1] == "entry"]
        assert len(entry_pushes) == 1
        assert entry_pushes[0].args[0] == "RIGHT-1234"
        assert "WRONG-0001" not in [c.args[0] for c in mock_pms.await_args_list]

    # ── UC1: a correct read arriving AFTER the crossing still wins ─────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_read_after_crossing_still_wins(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """CAM-23 line is at the ramp top next to the ANPR, so the correct read
        lands AFTER the crossing (recognition lag). That late read must stay in
        the SAME burst and win — not be split off as a new car."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle(plate="RIGHT-1234")
        db = make_db()

        # Wrong early read → crossing confirms → correct read arrives afterwards.
        await handle_anpr_event(make_anpr_event(plate="WRONG-0001", pic_num=1), db)
        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        await handle_anpr_event(make_anpr_event(plate="RIGHT-1234", pic_num=2), db)

        # Still one open burst (the late read did NOT start a new car).
        assert len(_entry_bursts) == 1
        _force_idle()
        await flush_due_entry_bursts(db)

        db.add.assert_called_once()
        assert db.add.call_args[0][0].plate_number == "RIGHT-1234"

    # ── UC1: a fresh picNum (next car) closes the previous burst ───────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_picnum_reset_starts_new_car(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """A picNum reset (car B starts at pic 1) splits the buffer into two cars."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        # Car A: pics 1,2 — then car B: pic 1 (reset) → A closed, B opened.
        await handle_anpr_event(make_anpr_event(plate="CARA-0001", pic_num=1), db)
        await handle_anpr_event(make_anpr_event(plate="CARA-0002", pic_num=2), db)
        await handle_anpr_event(make_anpr_event(plate="CARB-0001", pic_num=1), db)

        # Two distinct bursts now exist (A closed for flush, B open).
        assert len(_entry_bursts) == 2

    # ── UC1: two-phase disabled — idle window alone commits ────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_idle_flush_without_confirmation(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """USE_CAM03_ENTRY_CONFIRMATION=False: a single read flushes on the idle
        window with no ramp crossing required."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        db.add.assert_not_called()
        _force_idle()
        await flush_due_entry_bursts(db)

        db.add.assert_called_once()
        assert db.add.call_args[0][0].plate_number == "ABC-1234"
        mock_open.assert_called_once()

    # ── UC1: two-phase enabled — unconfirmed burst is dropped at hard cap ──

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unconfirmed_burst_dropped(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """Two-phase: a burst that never gets a ramp crossing within the hard cap
        is a false trigger — dropped, never written."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        # Age the burst past ANPR_BURST_MAX_SECONDS with no confirmation.
        for b in _entry_bursts.values():
            b["first_event_time"] = facility_now_naive() - timedelta(seconds=999)
            b["last_read_at"] = facility_now_naive() - timedelta(seconds=999)
        await flush_due_entry_bursts(db)

        db.add.assert_not_called()
        assert len(_entry_bursts) == 0

    # ── UC1: a crossing must not confirm a closed/stale burst ──────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_crossing_ignores_closed_burst(self, mock_vs, mock_alert, mock_settings):
        """A crossing must not confirm a force_flush (closed previous-car) burst —
        that car's own crossing already fired, so this one belongs to the next
        car → held pending, closed burst left unconfirmed."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await handle_anpr_event(make_anpr_event(plate="CARA-0001", pic_num=1), db)
        for b in _entry_bursts.values():   # simulate the burst being closed
            b["force_flush"] = True

        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")

        assert len(_pending_crossings) == 1
        assert all(not b["confirmed"] for b in _entry_bursts.values())

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_crossing_ignores_stale_burst(self, mock_vs, mock_alert, mock_settings):
        """A crossing must not revive a stale (older than the hard cap) leftover
        burst into a real entry — it's a false ANPR trigger, not this car."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        for b in _entry_bursts.values():   # age it past ANPR_BURST_MAX_SECONDS
            b["first_event_time"] = facility_now_naive() - timedelta(seconds=999)

        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")

        assert len(_pending_crossings) == 1
        assert all(not b["confirmed"] for b in _entry_bursts.values())

    # ── UC1: CAM-03 confirms a still-open burst ────────────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_cam03_confirms_and_forwards_snapshot(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """CAM-03 confirming an open burst forwards its snapshot to the PMS under
        the B-entry marker, after the winning-plate gate image."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        await confirm_pending_entry(db, cam03_snapshot="detection_images/cam03.jpg")
        _force_idle()
        await flush_due_entry_bursts(db)

        # The last PMS call carries CAM-03's image under the B-entry marker.
        assert mock_pms.await_args.args == ("ABC-1234", CAM03_PMS_DIRECTION)
        assert mock_pms.await_args.kwargs["image_path"] == "detection_images/cam03.jpg"

    # ── UC1: BOTH CAM-23 and CAM-03 images reach the PMS ───────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_both_confirm_images_before_flush(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """CAM-23 and CAM-03 both confirming before flush → each image is sent to
        the PMS under its own direction marker."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")  # CAM-03 before flush
        _force_idle()
        await flush_due_entry_bursts(db)

        by_direction = {c.args[1]: c.kwargs.get("image_path") for c in mock_pms.await_args_list}
        assert by_direction.get("ramp-entry") == "ramp.jpg"   # CAM-23
        assert by_direction.get("B-entry") == "garage.jpg"    # CAM-03

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_cam03_after_flush_attaches_image(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """The real ordering: CAM-03 fires AFTER the entry flushed → its image is
        matched to the just-written entry and forwarded under B-entry."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        _force_idle()
        await flush_due_entry_bursts(db)     # entry written, CAM-23 image sent
        assert len(_entry_bursts) == 0

        mock_pms.reset_mock()
        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")  # CAM-03 late
        # The forward is detached from the caller's DB transaction (it runs as a
        # background task), so wait for it before asserting.
        await drain_background_forwards()

        mock_pms.assert_awaited_once()
        assert mock_pms.await_args.args == ("ABC-1234", "B-entry")
        assert mock_pms.await_args.kwargs["image_path"] == "garage.jpg"

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_cam03_after_flush_no_recent_entry(self, mock_alert, mock_settings, mock_pms):
        """A late CAM-03 crossing with no recently-flushed entry to attach to is a
        no-op — no PMS call, no error."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        mock_pms.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_entry_is_committed_before_any_network_forward(
        self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms
    ):
        """Regression (2026-07-12 freeze): the entry row + parking session must be
        COMMITTED before the flush awaits any PMS forward.

        Holding those write locks across a network await deadlocked the service:
        the sync pyodbc driver runs on the event loop, so a concurrent handler
        blocking on the uncommitted rows froze the loop that would have resumed
        this coroutine to commit. Nothing could then read entry_exit_log."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        committed_before_forward = []

        async def pms_side_effect(plate, direction, image_path=None):
            committed_before_forward.append(db.commit.called)
        mock_pms.side_effect = pms_side_effect

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        _force_idle()
        await flush_due_entry_bursts(db)
        await drain_background_forwards()

        assert committed_before_forward, "expected at least one PMS forward"
        assert all(committed_before_forward), (
            "a PMS forward was awaited while the entry write was still "
            "uncommitted — this is the deadlock that froze the backend"
        )

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_cam03_during_flush_window_attaches(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """Regression: CAM-03 firing DURING the flush's awaited PMS forwards must
        still attach — the entry is registered in _recent_entries BEFORE the
        network I/O, so the mid-flush crossing finds it."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        fired = {"done": False}

        async def pms_side_effect(plate, direction, image_path=None):
            # Simulate CAM-03 crossing landing during the first (entry) forward.
            if direction == "entry" and not fired["done"]:
                fired["done"] = True
                await confirm_pending_entry(db, cam03_snapshot="garage.jpg")
        mock_pms.side_effect = pms_side_effect

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        _force_idle()
        await flush_due_entry_bursts(db)
        await drain_background_forwards()   # CAM-03's forward is detached from the txn

        by_direction = {c.args[1]: c.kwargs.get("image_path") for c in mock_pms.await_args_list}
        assert by_direction.get("B-entry") == "garage.jpg"   # CAM-03 attached mid-flush
        assert by_direction.get("ramp-entry") == "ramp.jpg"  # CAM-23 still forwarded

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_cam03_no_snapshot_no_extra_forward(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """When CAM-03 has no snapshot, no B-entry forward is attempted."""
        configure_settings(mock_settings, two_phase=True)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        await confirm_pending_entry(db, cam03_snapshot=None)
        _force_idle()
        await flush_due_entry_bursts(db)

        markers = [c.args[1] for c in mock_pms.await_args_list if len(c.args) > 1]
        assert CAM03_PMS_DIRECTION not in markers

    # ── UC1: silent entry (ramp crossing, no plate read) ───────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_cam23_silent_entry_alert(self, mock_alert, mock_settings):
        """A CAM-23 ramp crossing with no buffered ANPR read becomes a
        silent-entry alert once the window elapses."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        assert len(_pending_crossings) == 1
        _age_pending_crossings()
        await flush_due_entry_bursts(db)

        mock_alert.assert_called_once()
        assert mock_alert.call_args[1]["alert_type"] == "silent_entry"

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_cam03_no_burst_is_noop_not_silent(self, mock_alert, mock_settings):
        """CAM-03 fires deep in the garage, usually after the burst already
        flushed — finding no open burst is normal, NOT a silent entry."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="cam03.jpg")
        assert len(_pending_crossings) == 0  # CAM-03 never queues a silent crossing
        await flush_due_entry_bursts(db)

        mock_alert.assert_not_called()

    # ── Guard: no plate ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_no_plate_skipped(self, mock_vs, mock_alert):
        """ANPR events without a plate number are silently ignored."""
        await handle_anpr_event(make_anpr_event(plate=None), MagicMock())
        mock_alert.assert_not_called()

    # ── UC4: Alert on unregistered vehicle at entry (at flush) ─────────────

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.open_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_unregistered_entry_triggers_alert(self, mock_vs, mock_open, mock_alert, mock_settings, mock_pms):
        """UC4: Unregistered vehicle at entry gate triggers an alert at flush time."""
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle(registered=False)
        db = make_db()

        await handle_anpr_event(make_anpr_event(pic_num=1), db)
        _force_idle()
        await flush_due_entry_bursts(db)

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

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_exit_forward_carries_original_aware_camera_timestamp(
        self,
        mock_vs,
        mock_close,
        mock_alert,
        mock_settings,
        mock_pms,
    ):
        configure_settings(mock_settings, two_phase=False)
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        captured_at = datetime(2026, 7, 21, 9, 0, tzinfo=timezone(timedelta(hours=3)))

        forward = await handle_anpr_event(
            make_anpr_event(gate="exit", trigger_time=captured_at),
            make_db(first_side_effect=[None, None]),
        )

        assert forward is not None
        assert forward.captured_at == captured_at
        await forward.deliver()
        mock_pms.assert_awaited_once_with(
            "ABC-1234",
            "exit",
            image_path=None,
            captured_at=captured_at,
        )

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.core_backend_client.notify_pms_anpr", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_duplicate_exit_replays_va_forward_without_repeating_pms_mutation(
        self,
        mock_vs,
        mock_close,
        mock_alert,
        mock_settings,
        mock_pms,
    ):
        """A camera retry heals a crash after exit commit but before VA delivery."""
        configure_settings(mock_settings, two_phase=False)
        captured_at = datetime(
            2026,
            7,
            21,
            9,
            0,
            tzinfo=timezone(timedelta(hours=3)),
        )
        event = make_anpr_event(gate="exit", trigger_time=captured_at)
        event.local_snapshot_path = "/tmp/retried-exit.jpg"
        db = make_db(first_side_effect=[MagicMock()])

        forward = await handle_anpr_event(event, db)

        assert forward is not None
        assert forward.plate == "ABC-1234"
        assert forward.direction == "exit"
        assert forward.image_path == "/tmp/retried-exit.jpg"
        assert forward.captured_at == captured_at
        db.add.assert_not_called()
        mock_vs.ensure_unregistered_vehicle.assert_not_called()
        mock_close.assert_not_called()
        mock_alert.assert_not_awaited()

        await forward.deliver()
        mock_pms.assert_awaited_once_with(
            "ABC-1234",
            "exit",
            image_path="/tmp/retried-exit.jpg",
            captured_at=captured_at,
        )

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.acquire_plate_transaction_lock")
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_exit_locks_plate_before_state_queries(
        self,
        mock_vs,
        mock_close,
        mock_alert,
        mock_settings,
        mock_lock,
    ):
        configure_settings(mock_settings, two_phase=False)
        mock_settings.ENTRY_V2_MODE = "authoritative"
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()
        db = make_db(first_side_effect=[None, None])
        query = db.query
        calls = []
        mock_lock.side_effect = lambda *_: calls.append("lock")
        db.query.side_effect = lambda *_: (calls.append("query"), query.return_value)[1]

        await handle_anpr_event(make_anpr_event(gate="exit"), db)

        mock_lock.assert_called_once_with(db, "ABC-1234")
        assert calls[:2] == ["lock", "query"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["off", "shadow"])
    @patch("app.services.entry_exit_service.acquire_plate_transaction_lock")
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.parking_session_service.close_session")
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_non_authoritative_exit_does_not_take_v2_plate_lock(
        self,
        mock_vs,
        mock_close,
        mock_alert,
        mock_settings,
        mock_lock,
        mode,
    ):
        configure_settings(mock_settings, two_phase=False)
        mock_settings.ENTRY_V2_MODE = mode
        mock_vs.ensure_unregistered_vehicle.return_value = make_vehicle()

        await handle_anpr_event(
            make_anpr_event(gate="exit"),
            make_db(first_side_effect=[None, None]),
        )

        mock_lock.assert_not_called()

    # ── UC2: Parking duration (exit path — unchanged) ─────────────────────

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
