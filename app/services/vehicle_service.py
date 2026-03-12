# app/services/vehicle_service.py
"""
UC4: Vehicle Identity & Classification — business logic.
Handles registered vehicle management and lookup.
"""

from typing import Optional, List
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.vehicle import Vehicle
from app.utils.logger import get_logger

# Note: Assuming vehicle_repo is implemented in app.repositories
try:
    from app.repositories.vehicle_repository import vehicle_repo
except ImportError:
    vehicle_repo = None

logger = get_logger(__name__)


def lookup_vehicle(db: Session, plate: str) -> Optional[Vehicle]:
    """Find a registered vehicle by plate number."""
    if vehicle_repo:
        return vehicle_repo.get_by_plate(db, plate)
    return db.query(Vehicle).filter(Vehicle.plate_number == plate).first()


def is_registered(db: Session, plate: str) -> bool:
    """Check if a plate number exists in the system."""
    return lookup_vehicle(db, plate) is not None


def list_vehicles(db: Session, vehicle_type: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> List[Vehicle]:
    """Return paginated vehicle list with optional type filter."""
    if vehicle_repo:
        return vehicle_repo.list_all(db, vehicle_type=vehicle_type, limit=limit, offset=offset)
    
    query = db.query(Vehicle)
    if vehicle_type:
        query = query.filter(Vehicle.vehicle_type == vehicle_type)
    return query.offset(offset).limit(limit).all()


def register_vehicle(db: Session, plate_number: str, owner_name: str,
                     vehicle_type: str, employee_id: Optional[str] = None,
                     notes: Optional[str] = None) -> Vehicle:
    """Register a new vehicle. Raises ValueError if plate already exists."""
    if is_registered(db, plate_number):
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
    
    db.add(vehicle)
    logger.info(f"Registered vehicle: {plate_number} ({vehicle_type}) owner={owner_name}")
    return vehicle


def remove_vehicle(db: Session, plate: str) -> None:
    """Remove a vehicle by plate. Raises ValueError if not found."""
    vehicle = lookup_vehicle(db, plate)
    if not vehicle:
        raise ValueError(f"Vehicle with plate {plate} not found")
    
    db.delete(vehicle)
    logger.info(f"Removed vehicle: {plate}")