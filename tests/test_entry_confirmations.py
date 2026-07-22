"""Transactional and authentication tests for VA entry confirmations."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.entry_exit_log import EntryExitLog
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle  # noqa: F401 - registers FK target
from app.routers.entry_confirmations import (
    confirm_entry,
    require_entry_v2_service_key,
)
from app.schemas.entry_confirmation import EntryConfirmationRequest
from app.services import parking_session_service
from app.services.parking_session_service import REENTRY_RECONCILIATION_CAMERA_ID
from app.services.entry_confirmation_service import (
    _acquire_mssql_application_lock,
    _lock_resource,
    _plate_lock_resource,
)
from app.services.entry_state_lock import (
    EntryStateLockUnavailable,
    plate_lock_resource,
)
from app.services.entry_exit_service import handle_anpr_event
from app.services.event_parser import ParsedCameraEvent


engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
FACILITY_TZ = timezone(timedelta(hours=3))


def _captured_at(*args) -> datetime:
    return datetime(*args, tzinfo=FACILITY_TZ)


@pytest.fixture
def db(monkeypatch):
    # SQLite is an explicit unit-test substitute. Production authoritative mode
    # is required to use SQL Server application locks and fails closed otherwise.
    monkeypatch.setattr(
        "app.services.entry_confirmation_service._acquire_mssql_application_lock",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.entry_exit_service.acquire_plate_transaction_lock",
        lambda *_args, **_kwargs: None,
    )
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def _body(**overrides):
    values = {
        "decision_id": "decision-1",
        "status": "confirmed",
        "canonical_plate": "ABC-1234",
        "attempt_id": "attempt-1",
        "crossing_id": "crossing-1",
        "entry_camera_id": "CAM-ENTRY",
        "entry_captured_at": _captured_at(2026, 7, 20, 12, 0),
        "reported_plate": "A8C-1234",
        "plate_confidence": 96,
        "corrected": True,
    }
    values.update(overrides)
    return EntryConfirmationRequest(**values)


def _exit_event(trigger_time: datetime) -> ParsedCameraEvent:
    return ParsedCameraEvent(
        camera_id="CAM-EXIT",
        device_serial="EXIT-SERIAL",
        channel_id=1,
        event_type="ANPR",
        detection_target="vehicle",
        region_id="exit",
        channel_name="Exit",
        trigger_time=trigger_time,
        raw_xml="<event />",
        plate_number="ABC-1234",
        gate="exit",
    )


def test_confirmation_creates_one_log_and_session_without_snapshots(db, monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")

    response = confirm_entry(_body(), None, db)

    assert response.result == "created"
    log_entry = db.query(EntryExitLog).one()
    session = db.query(ParkingSession).one()
    assert log_entry.plate_number == "ABC-1234"
    assert log_entry.snapshot_path is None
    assert session.plate_number == "ABC-1234"
    assert session.entry_snapshot_path is None
    assert session.status == "open"


def test_confirmation_replay_reuses_same_rows(db, monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    first = confirm_entry(_body(), None, db)

    replay = confirm_entry(_body(), None, db)

    assert first.result == "created"
    assert replay.result == "duplicate"
    assert replay.entry_log_id == first.entry_log_id
    assert replay.session_id == first.session_id
    assert db.query(EntryExitLog).count() == 1
    assert db.query(ParkingSession).count() == 1


def test_confirmation_and_exit_use_same_normalized_plate_lock_key():
    body = _body(canonical_plate="abc-1234")

    assert _plate_lock_resource(body) == plate_lock_resource("ABC-1234")


def test_confirmation_lock_serializes_timestamp_tolerance_window():
    first = _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 0, 0, 1000))
    near_duplicate = _body(
        decision_id="decision-2",
        entry_captured_at=_captured_at(2026, 7, 20, 12, 0, 0, 9000),
    )
    other_camera = _body(entry_camera_id="CAM-OTHER")

    assert _lock_resource(first) == _lock_resource(near_duplicate)
    assert _lock_resource(first) != _lock_resource(other_camera)


def test_mssql_application_lock_uses_bounded_configured_timeout(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_APPLOCK_TIMEOUT_MS", 750)
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "mssql"

    _acquire_mssql_application_lock(db, "entry-v2:test")

    _, params = db.execute.call_args.args
    assert params == {"resource": "entry-v2:test", "lock_timeout": 750}


def test_mssql_application_lock_failure_is_retryable_state_error():
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "mssql"
    db.execute.side_effect = RuntimeError("lock timeout")

    with pytest.raises(EntryStateLockUnavailable):
        _acquire_mssql_application_lock(db, "entry-v2:test")


def test_authoritative_non_mssql_application_lock_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "sqlite"

    with pytest.raises(EntryStateLockUnavailable, match="requires the mssql"):
        _acquire_mssql_application_lock(db, "entry-v2:test")

    db.execute.assert_not_called()


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_non_authoritative_non_mssql_application_lock_is_explicit_noop(
    monkeypatch,
    mode,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", mode)
    db = MagicMock()
    db.get_bind.return_value.dialect.name = "sqlite"

    _acquire_mssql_application_lock(db, "entry-v2:test")

    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_authoritative_non_mssql_fails_before_startup_work(monkeypatch):
    import app.main as main

    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(main.engine.dialect, "name", "sqlite")
    create_tables = MagicMock()
    monkeypatch.setattr(main, "create_tables", create_tables)

    with pytest.raises(EntryStateLockUnavailable, match="requires the mssql"):
        await main.startup()

    create_tables.assert_not_called()


def test_confirmation_lock_contention_returns_retryable_503(db, monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(
        "app.services.entry_confirmation_service._acquire_mssql_application_lock",
        MagicMock(side_effect=EntryStateLockUnavailable("busy")),
    )

    with pytest.raises(HTTPException) as exc:
        confirm_entry(_body(), None, db)

    assert exc.value.status_code == 503
    assert exc.value.headers == {"Retry-After": "1"}
    assert db.query(EntryExitLog).count() == 0
    assert db.query(ParkingSession).count() == 0


def test_same_camera_timestamp_with_different_plate_creates_distinct_rows(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    first = confirm_entry(_body(), None, db)

    second = confirm_entry(
        _body(decision_id="decision-2", canonical_plate="XYZ-9876"),
        None,
        db,
    )

    assert second.result == "created"
    assert second.entry_log_id != first.entry_log_id
    assert second.plate_number == "XYZ-9876"
    assert db.query(EntryExitLog).count() == 2
    assert db.query(ParkingSession).count() == 2


def test_preexisting_same_time_other_plate_does_not_capture_replay_key(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    event_time = datetime(2026, 7, 20, 12, 0)
    db.add(
        EntryExitLog(
            plate_number="XYZ-9876",
            gate="entry",
            camera_id="CAM-ENTRY",
            event_time=event_time,
        )
    )
    db.commit()

    response = confirm_entry(
        _body(entry_captured_at=event_time.replace(tzinfo=FACILITY_TZ)),
        None,
        db,
    )

    assert response.result == "created"
    assert response.plate_number == "ABC-1234"
    assert db.query(EntryExitLog).count() == 2
    assert db.query(ParkingSession).count() == 1


def test_validated_reentry_reconciles_older_open_stay_and_retry_is_idempotent(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    first_body = _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 0))
    first = confirm_entry(first_body, None, db)
    old_session = db.get(ParkingSession, first.session_id)
    vehicle = db.get(Vehicle, old_session.vehicle_id)
    old_session.slot_id = "SLOT-B1-12"
    old_session.slot_number = "B1-12"
    old_session.floor = "B1"
    vehicle.current_slot_id = old_session.slot_id
    vehicle.floor = "B1"
    db.commit()

    reentry_time = datetime(2026, 7, 20, 12, 5)
    second_body = _body(
        decision_id="decision-2",
        attempt_id="attempt-2",
        crossing_id="crossing-2",
        entry_captured_at=reentry_time.replace(tzinfo=FACILITY_TZ),
    )
    second = confirm_entry(second_body, None, db)
    replay = confirm_entry(second_body, None, db)

    assert second.result == "created"
    assert replay.result == "duplicate"
    assert replay.entry_log_id == second.entry_log_id
    assert replay.session_id == second.session_id
    assert db.query(EntryExitLog).count() == 2
    assert db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").count() == 0
    assert db.query(ParkingSession).count() == 2

    db.refresh(old_session)
    assert old_session.status == "closed"
    assert old_session.exit_time == reentry_time
    assert old_session.exit_camera_id == REENTRY_RECONCILIATION_CAMERA_ID
    assert old_session.duration_seconds == 300
    assert old_session.slot_left_at == reentry_time

    new_session = db.get(ParkingSession, second.session_id)
    assert new_session.status == "open"
    assert new_session.entry_time == reentry_time
    assert new_session.entry_camera_id == "CAM-ENTRY"
    assert db.query(ParkingSession).filter(ParkingSession.status == "open").count() == 1
    db.refresh(vehicle)
    assert vehicle.current_slot_id is None
    assert vehicle.floor is None


def test_out_of_order_confirmation_is_terminal_without_touching_newer_stay(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    current = confirm_entry(
        _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 5)),
        None,
        db,
    )

    response = confirm_entry(
        _body(
            decision_id="decision-delayed",
            attempt_id="attempt-delayed",
            crossing_id="crossing-delayed",
            entry_camera_id="CAM-OTHER-ENTRY",
            entry_captured_at=_captured_at(2026, 7, 20, 12, 0),
        ),
        None,
        db,
    )

    assert response.model_dump() == {
        "decision_id": "decision-delayed",
        "status": "confirmed",
        "result": "superseded_by_newer_entry",
        "plate_number": "ABC-1234",
        "entry_log_id": None,
        "session_id": None,
    }
    assert db.query(EntryExitLog).count() == 1
    assert db.query(ParkingSession).count() == 1
    session = db.get(ParkingSession, current.session_id)
    assert session.status == "open"
    assert session.exit_time is None


def test_reentry_reconciliation_rolls_back_if_new_session_cannot_open(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    first = confirm_entry(
        _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 0)),
        None,
        db,
    )

    with (
        patch(
            "app.services.entry_confirmation_service."
            "parking_session_service.open_session",
            side_effect=RuntimeError("simulated session insert failure"),
        ),
        pytest.raises(RuntimeError, match="simulated session insert failure"),
    ):
        confirm_entry(
            _body(
                decision_id="decision-2",
                attempt_id="attempt-2",
                crossing_id="crossing-2",
                entry_captured_at=_captured_at(2026, 7, 20, 12, 5),
            ),
            None,
            db,
        )

    db.expire_all()
    old_session = db.get(ParkingSession, first.session_id)
    assert old_session.status == "open"
    assert old_session.exit_time is None
    assert old_session.exit_camera_id is None
    assert db.query(EntryExitLog).count() == 1
    assert db.query(ParkingSession).count() == 1


def test_delayed_confirmation_after_exit_does_not_create_ghost_stay(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    body = _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 0))
    db.add(
        EntryExitLog(
            plate_number="ABC-1234",
            gate="exit",
            camera_id="CAM-EXIT",
            event_time=datetime(2026, 7, 20, 12, 5),
        )
    )
    db.commit()

    response = confirm_entry(body, None, db)

    assert response.model_dump() == {
        "decision_id": "decision-1",
        "status": "confirmed",
        "result": "stale_after_exit",
        "plate_number": "ABC-1234",
        "entry_log_id": None,
        "session_id": None,
    }
    assert db.query(EntryExitLog).count() == 1
    assert db.query(EntryExitLog).one().gate == "exit"
    assert db.query(ParkingSession).count() == 0


def test_replay_does_not_repair_missing_session_after_committed_exit(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    entry_time = datetime(2026, 7, 20, 12, 0)
    db.add_all(
        [
            EntryExitLog(
                plate_number="ABC-1234",
                gate="entry",
                camera_id="CAM-ENTRY",
                event_time=entry_time,
            ),
            EntryExitLog(
                plate_number="ABC-1234",
                gate="exit",
                camera_id="CAM-EXIT",
                event_time=datetime(2026, 7, 20, 12, 5),
            ),
        ]
    )
    db.commit()

    response = confirm_entry(
        _body(entry_captured_at=entry_time.replace(tzinfo=FACILITY_TZ)),
        None,
        db,
    )

    assert response.result == "stale_after_exit"
    assert response.entry_log_id is None
    assert response.session_id is None
    assert db.query(EntryExitLog).count() == 2
    assert db.query(ParkingSession).count() == 0


@pytest.mark.asyncio
async def test_lost_ack_replay_after_exit_is_terminal_stale(db, monkeypatch):
    """A retry cannot reopen or report a stay that exited after first commit."""
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-EXIT": {"gate": "exit"}})
    body = _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 0))

    # The first transaction committed, but simulate its HTTP response being lost.
    first = confirm_entry(body, None, db)
    exit_event = _exit_event(datetime(2026, 7, 20, 12, 5))
    with patch(
        "app.services.entry_exit_service.create_alert",
        new_callable=AsyncMock,
    ):
        post_commit_forward = await handle_anpr_event(exit_event, db)
    db.commit()
    assert post_commit_forward is not None

    replay = confirm_entry(body, None, db)

    assert first.result == "created"
    assert replay.model_dump() == {
        "decision_id": "decision-1",
        "status": "confirmed",
        "result": "stale_after_exit",
        "plate_number": "ABC-1234",
        "entry_log_id": None,
        "session_id": None,
    }
    assert db.query(EntryExitLog).count() == 2
    exit_log = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    session = db.query(ParkingSession).one()
    assert session.id == first.session_id
    assert session.status == "closed"
    assert session.exit_time == exit_log.event_time


@pytest.mark.asyncio
async def test_delayed_old_exit_corrects_reconciled_stay_not_new_stay(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-EXIT": {"gate": "exit"}})
    old = confirm_entry(
        _body(entry_captured_at=_captured_at(2026, 7, 20, 8, 0)),
        None,
        db,
    )
    reentry_body = _body(
        decision_id="decision-reentry",
        attempt_id="attempt-reentry",
        crossing_id="crossing-reentry",
        entry_captured_at=_captured_at(2026, 7, 20, 10, 0),
    )
    current = confirm_entry(reentry_body, None, db)

    # The physical exit belonged to the old stay but its camera event arrived
    # after the re-entry confirmation transaction had already committed.
    with patch(
        "app.services.entry_exit_service.create_alert",
        new_callable=AsyncMock,
    ):
        forward = await handle_anpr_event(
            _exit_event(datetime(2026, 7, 20, 9, 0)),
            db,
        )
    db.commit()

    assert forward is not None
    old_session = db.get(ParkingSession, old.session_id)
    current_session = db.get(ParkingSession, current.session_id)
    assert old_session.status == "closed"
    assert old_session.exit_time == datetime(2026, 7, 20, 9, 0)
    assert old_session.exit_camera_id == "CAM-EXIT"
    assert old_session.duration_seconds == 3600
    assert current_session.status == "open"
    assert current_session.exit_time is None

    old_log = db.get(EntryExitLog, old.entry_log_id)
    current_log = db.get(EntryExitLog, current.entry_log_id)
    exit_log = db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").one()
    assert exit_log.matched_entry_id == old_log.id
    assert old_log.matched_entry_id == exit_log.id
    assert current_log.matched_entry_id is None

    replay = confirm_entry(reentry_body, None, db)
    assert replay.result == "duplicate"
    assert replay.session_id == current.session_id


@pytest.mark.asyncio
async def test_old_exit_committed_before_reentry_allows_new_open_stay(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    monkeypatch.setattr(settings, "CAMERAS", {"CAM-EXIT": {"gate": "exit"}})
    old = confirm_entry(
        _body(entry_captured_at=_captured_at(2026, 7, 20, 8, 0)),
        None,
        db,
    )
    with patch(
        "app.services.entry_exit_service.create_alert",
        new_callable=AsyncMock,
    ):
        await handle_anpr_event(
            _exit_event(datetime(2026, 7, 20, 9, 0)),
            db,
        )
    db.commit()

    current = confirm_entry(
        _body(
            decision_id="decision-reentry",
            attempt_id="attempt-reentry",
            crossing_id="crossing-reentry",
            entry_captured_at=_captured_at(2026, 7, 20, 10, 0),
        ),
        None,
        db,
    )

    old_session = db.get(ParkingSession, old.session_id)
    current_session = db.get(ParkingSession, current.session_id)
    assert old_session.exit_time == datetime(2026, 7, 20, 9, 0)
    assert old_session.exit_camera_id == "CAM-EXIT"
    assert current.result == "created"
    assert current_session.status == "open"
    assert current_session.entry_time == datetime(2026, 7, 20, 10, 0)
    assert db.query(ParkingSession).filter(ParkingSession.status == "open").count() == 1


def test_lost_ack_replay_after_closed_session_without_exit_log_is_stale(
    db,
    monkeypatch,
):
    """Closed session state alone is sufficient to make a replay terminal."""
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")
    body = _body(entry_captured_at=_captured_at(2026, 7, 20, 12, 0))
    first = confirm_entry(body, None, db)
    parking_session_service.close_session(
        db,
        plate_number="ABC-1234",
        event_time=datetime(2026, 7, 20, 12, 5),
        camera_id="CAM-EXIT",
        snapshot_path=None,
    )
    db.commit()

    replay = confirm_entry(body, None, db)

    assert replay.result == "stale_after_exit"
    assert replay.entry_log_id is None
    assert replay.session_id is None
    assert db.query(EntryExitLog).count() == 1
    assert db.query(ParkingSession).one().id == first.session_id
    assert db.query(ParkingSession).one().status == "closed"


@pytest.mark.parametrize("mode,expected", [("shadow", "shadowed"), ("authoritative", "abstained")])
def test_shadow_and_abstained_decisions_do_not_mutate(db, monkeypatch, mode, expected):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", mode)
    body = _body(
        status="abstained" if mode == "authoritative" else "confirmed",
        canonical_plate=None if mode == "authoritative" else "ABC-1234",
    )

    response = confirm_entry(body, None, db)

    assert response.result == expected
    assert db.query(EntryExitLog).count() == 0
    assert db.query(ParkingSession).count() == 0


def test_service_key_authentication_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "shared-secret")

    require_entry_v2_service_key("shared-secret")
    with pytest.raises(HTTPException) as exc:
        require_entry_v2_service_key("wrong")
    assert exc.value.status_code == 401

    monkeypatch.setattr(settings, "ENTRY_V2_SERVICE_KEY", "")
    with pytest.raises(HTTPException) as exc:
        require_entry_v2_service_key(None)
    assert exc.value.status_code == 503


def test_off_mode_rejects_confirmation_without_mutation(db, monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "off")

    with pytest.raises(HTTPException) as exc:
        confirm_entry(_body(), None, db)

    assert exc.value.status_code == 409
    assert db.query(EntryExitLog).count() == 0
    assert db.query(ParkingSession).count() == 0


def test_confirmation_contract_rejects_image_fields():
    values = _body().model_dump()
    values["image_base64"] = "not-allowed"

    with pytest.raises(ValidationError):
        EntryConfirmationRequest(**values)


def test_confirmation_contract_rejects_naive_entry_timestamp():
    values = _body().model_dump()
    values["entry_captured_at"] = datetime(2026, 7, 20, 12, 0)

    with pytest.raises(ValidationError, match="timezone"):
        EntryConfirmationRequest(**values)


@pytest.mark.parametrize(
    "captured_at",
    [
        datetime(1753, 1, 1, tzinfo=FACILITY_TZ),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=FACILITY_TZ),
    ],
)
def test_confirmation_contract_rejects_timestamps_outside_sql_safe_range(
    captured_at,
):
    values = _body().model_dump()
    values["entry_captured_at"] = captured_at

    with pytest.raises(ValidationError, match="SQL Server-safe range"):
        EntryConfirmationRequest(**values)


def test_invalid_plate_domain_error_is_terminal_422(db, monkeypatch):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")

    with pytest.raises(HTTPException) as exc:
        confirm_entry(_body(canonical_plate="???"), None, db)

    assert exc.value.status_code == 422
    assert db.query(EntryExitLog).count() == 0
    assert db.query(ParkingSession).count() == 0


def test_unexpected_value_error_from_confirmation_remains_retryable(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "ENTRY_V2_MODE", "authoritative")

    with patch(
        "app.routers.entry_confirmations.apply_confirmed_entry",
        side_effect=ValueError("internal regression"),
    ):
        with pytest.raises(ValueError, match="internal regression"):
            confirm_entry(_body(), None, db)

    assert db.query(EntryExitLog).count() == 0
    assert db.query(ParkingSession).count() == 0
