# app/repositories/vehicle_repository.py
"""
Vehicle data access layer (repository pattern).

All vehicle DB queries go through this class. When the data source
changes (e.g. external HR system API), only this file needs updating —
services and routers stay untouched.
"""

from typing import Optional
from sqlalchemy.orm import Session
from app.models.vehicle import Vehicle


class VehicleRepository:
    """Abstracts vehicle data access from the underlying storage."""

    def get_by_plate(self, db: Session, plate: str) -> Optional[Vehicle]:
        """Look up a vehicle by its license plate. Returns None if not found."""
        return db.query(Vehicle).filter(Vehicle.plate_number == plate).first()

    def is_registered(self, db: Session, plate: str) -> bool:
        """Check whether a plate number is registered in the system."""
        return self.get_by_plate(db, plate) is not None

    def list_all(self, db: Session, vehicle_type: Optional[str] = None,
                 limit: int = 50, offset: int = 0) -> list[Vehicle]:
        """Return vehicles with optional type filter and pagination."""
        q = db.query(Vehicle)
        if vehicle_type:
            q = q.filter(Vehicle.vehicle_type == vehicle_type)
        return q.order_by(Vehicle.id).offset(offset).limit(limit).all()

    def create(self, db: Session, vehicle: Vehicle) -> Vehicle:
        """Persist a new vehicle record."""
        db.add(vehicle)
        db.flush()  # assign ID without committing (transaction owned by caller)
        return vehicle

    def delete(self, db: Session, vehicle: Vehicle) -> None:
        """Remove a vehicle record."""
        db.delete(vehicle)


# Module-level singleton — swap this to change the data source globally.
vehicle_repo = VehicleRepository()
