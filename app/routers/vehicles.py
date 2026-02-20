# app/routers/vehicles.py
"""UC4: Vehicle Identity & Classification — CRUD for registered vehicles (Phase 2)"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.vehicle import Vehicle
from app.schemas.vehicle import VehicleCreate, VehicleOut
from datetime import datetime

router = APIRouter()


@router.get("/vehicles", response_model=list[VehicleOut], summary="UC4 — List registered vehicles")
def list_vehicles(vehicle_type: str = None, db: Session = Depends(get_db)):
    q = db.query(Vehicle)
    if vehicle_type:
        q = q.filter(Vehicle.vehicle_type == vehicle_type)
    return q.all()


@router.post("/vehicles", summary="UC4 — Register a new vehicle")
def register_vehicle(body: VehicleCreate, db: Session = Depends(get_db)):
    """Register an employee or visitor vehicle by plate number."""
    existing = db.query(Vehicle).filter(Vehicle.plate_number == body.plate_number).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Plate {body.plate_number} already registered")
    vehicle = Vehicle(
        plate_number=body.plate_number,
        owner_name=body.owner_name,
        vehicle_type=body.vehicle_type,
        employee_id=body.employee_id,
        notes=body.notes,
        is_registered=1,
        registered_at=datetime.utcnow(),
    )
    db.add(vehicle)
    db.commit()
    return {"status": "registered", "plate": body.plate_number}


@router.delete("/vehicles/{plate}", summary="UC4 — Remove a vehicle")
def remove_vehicle(plate: str, db: Session = Depends(get_db)):
    vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    db.delete(vehicle)
    db.commit()
    return {"status": "removed", "plate": plate}


@router.get("/vehicles/lookup/{plate}", summary="UC4 — Look up a plate number")
def lookup_vehicle(plate: str, db: Session = Depends(get_db)):
    vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    if not vehicle:
        return {"plate": plate, "status": "unknown", "registered": False}
    return {"plate": plate, "status": "known", "registered": True,
            "owner": vehicle.owner_name, "type": vehicle.vehicle_type}
