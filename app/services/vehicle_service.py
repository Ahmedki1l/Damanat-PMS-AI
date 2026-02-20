# app/services/vehicle_service.py
"""
🔜 Phase 2: Vehicle lookup and management helpers.
Used by entry_exit_service and vehicles router.
"""

from sqlalchemy.orm import Session
from app.models.vehicle import Vehicle
from app.utils.logger import get_logger

logger = get_logger(__name__)


def lookup_vehicle_by_plate(db: Session, plate_number: str):
    """Find a registered vehicle by plate number. Returns None if not found."""
    return db.query(Vehicle).filter(Vehicle.plate_number == plate_number).first()


def is_registered(db: Session, plate_number: str) -> bool:
    """Check if a plate number is registered in the system."""
    return lookup_vehicle_by_plate(db, plate_number) is not None
