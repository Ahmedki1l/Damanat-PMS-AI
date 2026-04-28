# tests/test_vehicle_service.py
"""Unit tests for vehicle_service (UC4)."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch
from app.models.vehicle import Vehicle
from app.services import vehicle_service


@pytest.fixture
def db():
    return MagicMock()


@pytest.fixture
def mock_vehicle():
    v = MagicMock(spec=Vehicle)
    v.plate_number = "A-1001"
    v.owner_name = "Ahmed Hassan"
    v.vehicle_type = "employee"
    v.is_employee = True
    v.title = "Employee"
    v.is_registered = True
    v.registered_at = None
    return v


class TestVehicleService:
    @patch("app.services.vehicle_service.vehicle_repo")
    def test_lookup_found(self, mock_repo, db, mock_vehicle):
        mock_repo.get_by_plate.return_value = mock_vehicle
        result = vehicle_service.lookup_vehicle(db, "A-1001")
        assert result == mock_vehicle
        mock_repo.get_by_plate.assert_called_once_with(db, "A-1001")

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_lookup_not_found(self, mock_repo, db):
        mock_repo.get_by_plate.return_value = None
        result = vehicle_service.lookup_vehicle(db, "UNKNOWN")
        assert result is None

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_is_registered(self, mock_repo, db):
        mock_repo.is_registered.return_value = True
        assert vehicle_service.is_registered(db, "A-1001") is True
        mock_repo.is_registered.assert_called_once_with(db, "A-1001")

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_register_new_vehicle(self, mock_repo, db):
        mock_repo.get_by_plate.return_value = None  # plate not taken
        mock_repo.create.side_effect = lambda db, v: v

        result = vehicle_service.register_vehicle(
            db, plate_number="X-9999", owner_name="Test User",
            vehicle_type="visitor",
        )
        assert result.plate_number == "X-9999"
        assert result.is_registered is True
        assert result.is_employee is False
        mock_repo.create.assert_called_once()

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_register_duplicate_raises(self, mock_repo, db, mock_vehicle):
        mock_repo.get_by_plate.return_value = mock_vehicle  # already exists

        with pytest.raises(ValueError, match="already registered"):
            vehicle_service.register_vehicle(
                db, plate_number="A-1001", owner_name="Dup",
                vehicle_type="employee",
            )
        mock_repo.create.assert_not_called()

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_ensure_unregistered_vehicle_creates_placeholder(self, mock_repo, db):
        mock_repo.get_by_plate.return_value = None
        mock_repo.create.side_effect = lambda db, v: v

        result = vehicle_service.ensure_unregistered_vehicle(db, "NEW-404")

        assert result.plate_number == "NEW-404"
        assert result.owner_name == "Unknown"
        assert result.vehicle_type == "unknown"
        assert result.is_registered is False
        assert result.notes == "Not registered"
        mock_repo.create.assert_called_once()

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_ensure_unregistered_vehicle_returns_existing(self, mock_repo, db, mock_vehicle):
        mock_repo.get_by_plate.return_value = mock_vehicle

        result = vehicle_service.ensure_unregistered_vehicle(db, "A-1001")

        assert result == mock_vehicle
        mock_repo.create.assert_not_called()

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_remove_vehicle(self, mock_repo, db, mock_vehicle):
        mock_repo.get_by_plate.return_value = mock_vehicle
        vehicle_service.remove_vehicle(db, "A-1001")
        mock_repo.delete.assert_called_once_with(db, mock_vehicle)

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_remove_not_found_raises(self, mock_repo, db):
        mock_repo.get_by_plate.return_value = None

        with pytest.raises(ValueError, match="not found"):
            vehicle_service.remove_vehicle(db, "UNKNOWN")
        mock_repo.delete.assert_not_called()

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_list_vehicles(self, mock_repo, db, mock_vehicle):
        mock_repo.list_all.return_value = [mock_vehicle]
        result = vehicle_service.list_vehicles(db, vehicle_type="employee", limit=10, offset=0)
        assert len(result) == 1
        mock_repo.list_all.assert_called_once_with(db, vehicle_type="employee", limit=10, offset=0)

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_update_vehicle(self, mock_repo, db, mock_vehicle):
        mock_repo.get_by_plate.return_value = mock_vehicle
        result = vehicle_service.update_vehicle(
            db,
            "A-1001",
            owner_name="Updated Owner",
            phone="12345",
            email="owner@example.com",
        )
        assert result.owner_name == "Updated Owner"
        assert result.phone == "12345"
        assert result.email == "owner@example.com"

    @patch("app.services.vehicle_service.vehicle_repo")
    def test_update_vehicle_can_mark_registered(self, mock_repo, db, mock_vehicle):
        mock_repo.get_by_plate.return_value = mock_vehicle

        result = vehicle_service.update_vehicle(
            db,
            "A-1001",
            is_registered=True,
        )

        assert result.is_registered is True
        assert result.registered_at is not None

