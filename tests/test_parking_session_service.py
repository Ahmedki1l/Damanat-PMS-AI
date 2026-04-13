"""Unit tests for parking session lifecycle and slot enrichment."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import parking_session_service


engine = create_engine("sqlite:///:memory:")


@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    dbapi_conn.isolation_level = None


@sa_event.listens_for(engine, "begin")
def _do_begin(conn):
    conn.exec_driver_sql("BEGIN")


TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def seed_vehicle(db):
    vehicle = Vehicle(
        plate_number="ABC-1234",
        owner_name="Driver",
        title="Employee",
        vehicle_type="employee",
        is_employee=True,
        is_registered=True,
    )
    db.add(vehicle)
    db.flush()
    return vehicle


def test_open_and_close_session(db):
    vehicle = seed_vehicle(db)
    entry_time = datetime(2026, 4, 11, 8, 0, 0)
    exit_time = datetime(2026, 4, 11, 10, 30, 0)

    session = parking_session_service.open_session(
        db,
        plate_number="ABC-1234",
        event_time=entry_time,
        camera_id="CAM-ENTRY",
        snapshot_path="entry.jpg",
        vehicle=vehicle,
    )
    db.flush()

    assert session.status == "open"
    assert session.entry_snapshot_path == "entry.jpg"

    closed = parking_session_service.close_session(
        db,
        plate_number="ABC-1234",
        event_time=exit_time,
        camera_id="CAM-EXIT",
        snapshot_path="exit.jpg",
    )
    db.flush()

    assert closed.status == "closed"
    assert closed.duration_seconds == 9000
    assert closed.exit_snapshot_path == "exit.jpg"


def test_bind_and_unbind_slot(db):
    vehicle = seed_vehicle(db)
    parking_session_service.open_session(
        db,
        plate_number="ABC-1234",
        event_time=datetime(2026, 4, 11, 8, 0, 0),
        camera_id="CAM-ENTRY",
        snapshot_path=None,
        vehicle=vehicle,
    )

    session = parking_session_service.bind_slot(
        db,
        plate_number="ABC-1234",
        slot_number="B1-12",
        zone_id="B1-PARKING",
        zone_name=None,
        floor=None,
        camera_id="CAM-04",
        parked_at=datetime(2026, 4, 11, 8, 10, 0),
        snapshot_path="slot.jpg",
    )
    assert session.slot_number == "B1-12"
    assert session.zone_name == "B1 Parking"
    assert session.floor == "B1"
    assert session.slot_snapshot_path == "slot.jpg"

    updated = parking_session_service.unbind_slot(
        db,
        plate_number="ABC-1234",
        camera_id="CAM-04",
        left_at=datetime(2026, 4, 11, 9, 0, 0),
        snapshot_path=None,
        slot_number="B1-12",
    )
    assert updated.slot_left_at == datetime(2026, 4, 11, 9, 0, 0)


def test_bind_slot_requires_open_session(db):
    with pytest.raises(LookupError):
        parking_session_service.bind_slot(
            db,
            plate_number="ABC-1234",
            slot_number="B1-12",
            zone_id="B1-PARKING",
            zone_name=None,
            floor=None,
            camera_id="CAM-04",
            parked_at=None,
            snapshot_path=None,
        )

