# app/services/vehicle_service.py
"""
UC4: Vehicle Identity & Classification — business logic.
All data access goes through VehicleRepository so the underlying
data source can be swapped without changing this file.
"""

from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.vehicle import Vehicle
from app.repositories.vehicle_repository import vehicle_repo
from app.utils.logger import get_logger

logger = get_logger(__name__)


def lookup_vehicle(db: Session, plate: str) -> Optional[Vehicle]:
    """Find a registered vehicle by plate number. Returns None if not found."""
    return vehicle_repo.get_by_plate(db, plate)


def is_registered(db: Session, plate: str) -> bool:
    """Check if a plate number is registered in the system."""
    return vehicle_repo.is_registered(db, plate)


def list_vehicles(db: Session, vehicle_type: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> list[Vehicle]:
    """Return paginated vehicle list with optional type filter."""
    return vehicle_repo.list_all(db, vehicle_type=vehicle_type, limit=limit, offset=offset)


def register_vehicle(db: Session, plate_number: str, owner_name: str,
                     vehicle_type: str, employee_id: Optional[str] = None,
                     notes: Optional[str] = None) -> Vehicle:
    """Register a new vehicle. Raises ValueError if plate already exists."""
    existing = vehicle_repo.get_by_plate(db, plate_number)
    if existing:
        raise ValueError(f"Plate {plate_number} already registered")

    vehicle = Vehicle(
        plate_number=plate_number,
        owner_name=owner_name,
        vehicle_type=vehicle_type,
        employee_id=employee_id,
        notes=notes,
        is_registered=True,
        registered_at=datetime.utcnow(),
    )
    vehicle_repo.create(db, vehicle)
    logger.info(f"Registered vehicle: {plate_number} ({vehicle_type}) owner={owner_name}")
    return vehicle


def remove_vehicle(db: Session, plate: str) -> None:
    """Remove a vehicle by plate. Raises ValueError if not found."""
    vehicle = vehicle_repo.get_by_plate(db, plate)
    if not vehicle:
        raise ValueError(f"Vehicle with plate {plate} not found")
    vehicle_repo.delete(db, vehicle)
    logger.info(f"Removed vehicle: {plate}")
