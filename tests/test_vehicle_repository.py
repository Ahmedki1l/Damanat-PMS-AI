# tests/test_vehicle_repository.py
"""Unit tests for VehicleRepository."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock
from app.models.vehicle import Vehicle
from app.repositories.vehicle_repository import VehicleRepository


@pytest.fixture
def repo():
    return VehicleRepository()


@pytest.fixture
def mock_vehicle():
    v = MagicMock(spec=Vehicle)
    v.plate_number = "A-1001"
    v.owner_name = "Ahmed Hassan"
    v.vehicle_type = "employee"
    return v


class TestVehicleRepository:
    def test_get_by_plate_found(self, repo, mock_vehicle):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vehicle

        result = repo.get_by_plate(db, "A-1001")
        assert result == mock_vehicle
        assert result.plate_number == "A-1001"

    def test_get_by_plate_not_found(self, repo):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        result = repo.get_by_plate(db, "UNKNOWN")
        assert result is None

    def test_is_registered_true(self, repo, mock_vehicle):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = mock_vehicle

        assert repo.is_registered(db, "A-1001") is True

    def test_is_registered_false(self, repo):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        assert repo.is_registered(db, "UNKNOWN") is False

    def test_list_all_no_filter(self, repo, mock_vehicle):
        db = MagicMock()
        db.query.return_value.order_by.return_value.offset.return_value.limit.return_value.all.return_value = [mock_vehicle]

        result = repo.list_all(db)
        assert len(result) == 1

    def test_list_all_with_type_filter(self, repo, mock_vehicle):
        db = MagicMock()
        chain = db.query.return_value.filter.return_value.order_by.return_value.offset.return_value.limit.return_value
        chain.all.return_value = [mock_vehicle]

        result = repo.list_all(db, vehicle_type="employee")
        assert len(result) == 1

    def test_create(self, repo):
        db = MagicMock()
        vehicle = Vehicle(plate_number="X-9999", owner_name="Test", vehicle_type="visitor")

        result = repo.create(db, vehicle)
        db.add.assert_called_once_with(vehicle)
        db.flush.assert_called_once()
        assert result == vehicle

    def test_delete(self, repo, mock_vehicle):
        db = MagicMock()
        repo.delete(db, mock_vehicle)
        db.delete.assert_called_once_with(mock_vehicle)

