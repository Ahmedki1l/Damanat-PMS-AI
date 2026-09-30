# tests/test_entry_cam03_rescue.py
"""CAM-03 rescuing a car CAM-23 missed, deduplicated by IDENTITY not by clock.

THE GAP. When the ramp cam (CAM-23) misses the line crossing, the ANPR burst is
never confirmed and the flusher drops it. CAM-03, deep in the garage, then sees
the very same car — and used to do nothing with that sighting, because its only
job was attaching an image to an entry that already existed. Nothing else
covered the gap either (`HIK_RECONCILE_OPEN_ENTRIES` is off), so the car was
lost.

HOW DUPLICATION IS AVOIDED. Not by asking whether something happened recently.
A plateless crossing carries no identity, so nothing at crossing time can
honestly answer "is this car already inside" — every such answer is a guess
about elapsed time. So CAM-03 holds its crossing without judging it, and the
question is settled at expiry against the one identity that exists: HikCentral's
GUID for a vehicle pass. Two cameras seeing one car produce ONE pass, so the
second crossing resolves to an already-consumed GUID and is dropped in silence.

The one precondition is a CAPABILITY check, not a guess about the car: the hold
happens only when HikCentral is authoritative, because nothing else can ever
answer it. And an unanswered hold stays silent — see TestARescueNeverAccuses.

Two cars can never share a pass, and `open_session` refuses a second open stay
per plate, so neither camera can manufacture a duplicate entry.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from time import monotonic
from unittest.mock import AsyncMock, patch

from app.config import facility_now_naive
from app.models.alert import Alert
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.services import entry_exit_service
from app.services.entry_exit_service import (
    confirm_entry_crossing,
    confirm_pending_entry,
    handle_anpr_event,
    _entry_bursts,
    _pending_crossings,
    _recent_entries,
)
from app.services.hikcentral import validation
from app.services.hikcentral.models import PLATE_SOURCE_HIK_RECOVERED
from test_entry_exit_service import (
    configure_settings,
    make_anpr_event,
    make_db,
)
from test_hikcentral_recovery import (  # noqa: F401 - db/hik fixtures reused
    _record,
    CROSSING_TIME,
    _crossing,
    _record,
    _stub_images,
    _stub_lookup,
    db,
    hik_authoritative,
)


class TestCam03HoldsItsSighting:
    """The crossing side: CAM-03 keeps what it saw, and judges nothing."""

    def setup_method(self):
        _entry_bursts.clear()
        _pending_crossings.clear()
        _recent_entries.clear()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_cam03_with_no_open_burst_holds_a_pending_crossing(
        self, mock_alert, mock_settings
    ):
        """The rescue: CAM-23 missed the crossing, the burst was dropped, and
        CAM-03 is the only camera that saw the car."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        assert len(_pending_crossings) == 1
        assert _pending_crossings[0]["source"] == "CAM-03"
        assert _pending_crossings[0]["snapshot"] == "garage.jpg"

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_a_recent_entry_does_not_suppress_the_hold(
        self, mock_alert, mock_settings
    ):
        """NO TIMESTAMP CONFIRMATION.

        An entry written moments ago is not evidence that it was THIS car —
        with two cars in play it is as likely to be the one ahead. So it does
        not silence the hold. The pass GUID settles it later, on identity.
        """
        configure_settings(mock_settings, two_phase=True)
        db = make_db()
        _recent_entries.append({
            "plate": "AAA-1111",
            "ts": facility_now_naive(),
            "sent_sources": set(),
        })

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        assert len(_pending_crossings) == 1

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_each_camera_keeps_its_own_sighting(
        self, mock_alert, mock_settings
    ):
        """Two cameras, two crossings — deliberately NOT merged.

        Collapsing them here would mean deciding they are the same car from
        their arrival times alone. They are reconciled at expiry instead, where
        the pass GUID can say so for real.
        """
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        assert [c["source"] for c in _pending_crossings] == ["CAM-23", "CAM-03"]

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_flag_off_restores_the_old_dead_end(self, mock_alert, mock_settings):
        """ENTRY_CAM03_CAN_RESCUE=False behaves exactly as before the change."""
        configure_settings(mock_settings, two_phase=True)
        mock_settings.ENTRY_CAM03_CAN_RESCUE = False
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        assert _pending_crossings == []

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    @patch("app.services.entry_exit_service.vehicle_service")
    async def test_a_late_burst_still_claims_a_cam03_crossing(
        self, mock_vs, mock_alert, mock_settings
    ):
        """A CAM-03 hold is a real crossing, so a burst arriving afterwards is
        confirmed by it rather than dropped as a ghost."""
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")
        await handle_anpr_event(make_anpr_event(plate="LATE-0001", pic_num=1), db)

        assert _pending_crossings == []
        buf = next(iter(_entry_bursts.values()))
        assert buf["confirmed"] is True
        assert buf["confirm_snapshots"] == {"CAM-03": "garage.jpg"}


class TestIdentitySettlesTheDuplicate:
    """The expiry side: HikCentral's pass GUID, not the clock, decides."""

    def test_consumed_pass_is_already_accounted(self):
        """Every record in the window already backs a gate event."""
        attempt = validation.RecoveryAttempt(
            outcome=None, records_found=1, already_consumed=1,
        )
        assert attempt.pass_already_accounted is True

    def test_an_empty_window_is_not_already_accounted(self):
        """HikCentral naming nothing is a car we cannot account for — a real
        silent entry. It must never be read as 'already entered'."""
        attempt = validation.RecoveryAttempt(
            outcome=None, records_found=0, already_consumed=0,
        )
        assert attempt.pass_already_accounted is False

    def test_a_partly_consumed_window_is_not_already_accounted(self):
        """One consumed pass and one fresh one means a second car really is
        unaccounted for; the fresh record is still a live candidate."""
        attempt = validation.RecoveryAttempt(
            outcome=None, records_found=2, already_consumed=1,
        )
        assert attempt.pass_already_accounted is False

    @pytest.mark.asyncio
    async def test_recovery_reports_the_consumed_count(self, monkeypatch, db):
        """The counts come back from the SAME lookup that decides recovery —
        answering 'already entered?' must not cost another HikCentral call."""
        db.add(HikValidation(
            direction="entry", guid="GUID-1",
            plate_source=PLATE_SOURCE_HIK_RECOVERED, matched=True,
            created_at=facility_now_naive(),
        ))
        db.flush()
        _stub_lookup(monkeypatch, [_record(guid="GUID-1")])

        attempt = await validation.recover_entry_pass(CROSSING_TIME, "CAM-03", db)

        assert attempt.outcome is None          # nothing to create
        assert attempt.pass_already_accounted   # ...because it is already in
        assert (attempt.records_found, attempt.already_consumed) == (1, 1)

    @pytest.mark.asyncio
    async def test_second_camera_makes_no_entry_and_raises_no_alert(
        self, monkeypatch, db
    ):
        """THE DUPLICATE GUARD, END TO END.

        CAM-23's crossing recovered the car and consumed its pass. CAM-03's
        crossing of the SAME car then expires. It must create nothing — and,
        just as importantly, must not report a silent entry for a car that was
        entered correctly.
        """
        _stub_images(monkeypatch)
        _stub_lookup(monkeypatch, [_record(guid="GUID-1")])

        recovered = await entry_exit_service._recover_silent_entry(
            db, _crossing(source="CAM-23")
        )
        db.commit()
        assert recovered is True
        assert db.query(ParkingSession).count() == 1

        alerts_before = db.query(Alert).count()
        handled = await entry_exit_service._recover_silent_entry(
            db, _crossing(source="CAM-03", snapshot="/snap/cam03.jpg")
        )
        db.commit()

        assert handled is True   # handled, so the caller raises no alert
        assert db.query(ParkingSession).count() == 1
        assert db.query(Alert).count() == alerts_before

    @pytest.mark.asyncio
    async def test_an_unknown_crossing_is_still_a_silent_entry(
        self, monkeypatch, db
    ):
        """The guard must not swallow genuine misses: an empty window leaves the
        crossing unhandled so the caller alerts."""
        _stub_lookup(monkeypatch, [])

        handled = await entry_exit_service._recover_silent_entry(
            db, _crossing(source="CAM-03")
        )

        assert handled is False


class TestARescueNeverAccuses:
    """A CAM-03 hold may SAVE a car; it must never report one as missing.

    CAM-03 has never been a silent-entry source. The ways a hold goes
    unanswered — HikCentral unreachable mid-flight, the pass not published yet,
    the layer switched off under us — are all failures to ASK, not evidence
    that a car slipped in. Alerting on them would convert every platform hiccup
    into one alert per car, which is strictly worse than the miss this feature
    exists to fix.
    """

    def setup_method(self):
        _entry_bursts.clear()
        _pending_crossings.clear()
        _recent_entries.clear()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_no_hold_when_hikcentral_cannot_adjudicate(
        self, mock_alert, mock_settings, monkeypatch
    ):
        """Shadow cannot create a session, so a hold could only ever expire."""
        configure_settings(mock_settings, two_phase=True)
        monkeypatch.setattr(
            "app.services.hikcentral.validation.settings.HIK_VALIDATION_MODE",
            "shadow",
        )
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        assert _pending_crossings == []
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_a_held_cam03_crossing_is_flagged_rescue_only(
        self, mock_alert, mock_settings
    ):
        configure_settings(mock_settings, two_phase=True)
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")

        assert _pending_crossings[0]["may_alert"] is False

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_unrecovered_cam03_hold_raises_no_alert(
        self, mock_alert, mock_settings, monkeypatch
    ):
        """THE REGRESSION. HikCentral is on but answers nothing — the outage
        case. The crossing expires unrecovered and must stay silent.
        """
        configure_settings(mock_settings, two_phase=True)
        _stub_lookup(monkeypatch, [])
        db = make_db()

        await confirm_pending_entry(db, cam03_snapshot="garage.jpg")
        assert len(_pending_crossings) == 1

        for crossing in _pending_crossings:
            crossing["expires_at_monotonic"] = monotonic() - 1.0
        await entry_exit_service.flush_due_entry_bursts(db)

        assert _pending_crossings == []
        mock_alert.assert_not_called()

    @pytest.mark.asyncio
    @patch("app.services.entry_exit_service.settings")
    @patch("app.services.entry_exit_service.create_alert", new_callable=AsyncMock)
    async def test_cam23_still_alerts(self, mock_alert, mock_settings, monkeypatch):
        """The silence is scoped to the rescue-only source. A ramp crossing with
        no plate is still a real silent entry and must still be reported."""
        configure_settings(mock_settings, two_phase=True)
        _stub_lookup(monkeypatch, [])
        db = make_db()

        await confirm_entry_crossing(db, snapshot="ramp.jpg", source_cam="CAM-23")
        for crossing in _pending_crossings:
            crossing["expires_at_monotonic"] = monotonic() - 1.0
        await entry_exit_service.flush_due_entry_bursts(db)

        mock_alert.assert_called_once()
        assert mock_alert.call_args[1]["alert_type"] == "silent_entry"


class TestPassAlreadyAccountedCounting:
    """Both sides of the comparison must count the same records."""

    @pytest.mark.asyncio
    async def test_a_plateless_record_does_not_hide_an_accounted_pass(
        self, monkeypatch, db, hik_authoritative
    ):
        """One unreadable plate in the window used to drop consumed(1) ==
        found(2) to false, decline recovery, and raise a silent-entry alert for
        a car that had entered correctly."""
        entered = _record(plate="ABC-1234", guid="GUID-ENTERED")
        unreadable = _record(plate=None, guid="GUID-NOPLATE")
        _stub_lookup(monkeypatch, [entered, unreadable])
        monkeypatch.setattr(validation, "guid_already_used", lambda _db, guid: True)

        attempt = await validation.recover_entry_pass(
            facility_now_naive(), "CAM-03", db
        )

        assert attempt.records_found == 1, "plateless records must not be counted"
        assert attempt.already_consumed == 1
        assert attempt.pass_already_accounted is True

    @pytest.mark.asyncio
    async def test_an_unconsumed_pass_is_still_not_accounted(
        self, monkeypatch, db, hik_authoritative
    ):
        _stub_lookup(monkeypatch, [_record(plate="ABC-1234", guid="GUID-NEW")])
        monkeypatch.setattr(validation, "guid_already_used", lambda _db, guid: False)

        attempt = await validation.recover_entry_pass(
            facility_now_naive(), "CAM-03", db
        )

        assert attempt.pass_already_accounted is False
