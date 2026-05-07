# app/services/vehicle_service.py
"""
UC4: Vehicle Identity & Classification — business logic.
Handles registered vehicle management and lookup.
"""

from typing import Optional, List
from datetime import datetime, UTC
from sqlalchemy.orm import Session
from app.models.vehicle import Vehicle
from app.utils.logger import get_logger
from app.config import facility_now_naive

# Note: Assuming vehicle_repo is implemented in app.repositories
try:
    from app.repositories.vehicle_repository import vehicle_repo
except ImportError:
    vehicle_repo = None

logger = get_logger(__name__)


def _default_title(vehicle_type: str) -> str:
    if vehicle_type.lower() == "employee":
        return "Employee"
    if vehicle_type.lower() == "visitor":
        return "Visitor"
    return "Vehicle"


def _default_is_employee(vehicle_type: str) -> bool:
    return vehicle_type.lower() == "employee"


def lookup_vehicle(db: Session, plate: str) -> Optional[Vehicle]:
    """Find a registered vehicle by plate number."""
    if vehicle_repo:
        return vehicle_repo.get_by_plate(db, plate)
    return db.query(Vehicle).filter(Vehicle.plate_number == plate).first()


def is_registered(db: Session, plate: str) -> bool:
    """Check if a plate number exists in the system."""
    if vehicle_repo:
        result = vehicle_repo.is_registered(db, plate)
        if isinstance(result, bool):
            return result
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
                     notes: Optional[str] = None, title: Optional[str] = None,
                     is_employee: Optional[bool] = None, phone: Optional[str] = None,
                     email: Optional[str] = None) -> Vehicle:
    """Register a new vehicle. Raises ValueError if plate already exists."""
    if is_registered(db, plate_number):
        raise ValueError(f"Plate {plate_number} already registered")

    vehicle = Vehicle(
        plate_number=plate_number,
        owner_name=owner_name,
        title=title or _default_title(vehicle_type),
        vehicle_type=vehicle_type,
        employee_id=employee_id,
        is_employee=_default_is_employee(vehicle_type) if is_employee is None else is_employee,
        phone=phone,
        email=email,
        notes=notes,
        is_registered=True,
        registered_at=facility_now_naive(),
    )

    if vehicle_repo:
        vehicle_repo.create(db, vehicle)
    else:
        db.add(vehicle)
    logger.info(f"Registered vehicle: {plate_number} ({vehicle_type}) owner={owner_name}")
    return vehicle


def ensure_unregistered_vehicle(db: Session, plate_number: str) -> Vehicle:
    """Return an existing vehicle or create a placeholder unregistered profile."""
    vehicle = lookup_vehicle(db, plate_number)
    if vehicle:
        return vehicle

    vehicle = Vehicle(
        plate_number=plate_number,
        owner_name="Unknown",
        title="Vehicle",
        vehicle_type="unknown",
        employee_id=None,
        is_employee=False,
        phone=None,
        email=None,
        notes="Not registered",
        is_registered=False,
        registered_at=None,
    )

    if vehicle_repo:
        vehicle_repo.create(db, vehicle)
    else:
        db.add(vehicle)
        db.flush()

    logger.info("Created placeholder vehicle for unregistered plate: %s", plate_number)
    return vehicle


def remove_vehicle(db: Session, plate: str) -> None:
    """Remove a vehicle by plate. Raises ValueError if not found."""
    vehicle = lookup_vehicle(db, plate)
    if not vehicle:
        raise ValueError(f"Vehicle with plate {plate} not found")
    
    if vehicle_repo:
        vehicle_repo.delete(db, vehicle)
    else:
        db.delete(vehicle)
    logger.info(f"Removed vehicle: {plate}")


def update_vehicle(
    db: Session,
    plate: str,
    *,
    owner_name: Optional[str] = None,
    title: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    employee_id: Optional[str] = None,
    is_employee: Optional[bool] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    notes: Optional[str] = None,
    is_registered: Optional[bool] = None,
) -> Vehicle:
    vehicle = lookup_vehicle(db, plate)
    if not vehicle:
        raise ValueError(f"Vehicle with plate {plate} not found")

    if owner_name is not None:
        vehicle.owner_name = owner_name
    if title is not None:
        vehicle.title = title
    if vehicle_type is not None:
        vehicle.vehicle_type = vehicle_type
        if title is None and not vehicle.title:
            vehicle.title = _default_title(vehicle_type)
    if employee_id is not None:
        vehicle.employee_id = employee_id
    if is_employee is not None:
        vehicle.is_employee = is_employee
    elif vehicle_type is not None:
        vehicle.is_employee = _default_is_employee(vehicle_type)
    if phone is not None:
        vehicle.phone = phone
    if email is not None:
        vehicle.email = email
    if notes is not None:
        vehicle.notes = notes
    if is_registered is not None:
        vehicle.is_registered = is_registered
        vehicle.registered_at = facility_now_naive() if is_registered else None

    logger.info("Updated vehicle profile: %s", plate)
    return vehicle

