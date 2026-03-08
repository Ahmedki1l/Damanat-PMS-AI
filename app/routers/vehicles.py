# app/routers/vehicles.py
"""UC4: Vehicle Identity & Classification — CRUD for registered vehicles."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.schemas.vehicle import VehicleCreate, VehicleResponse
from app.schemas.responses import VehicleActionResponse, VehicleLookupResponse
from app.services import vehicle_service

router = APIRouter()


@router.get("/vehicles", response_model=list[VehicleResponse], summary="UC4 — List registered vehicles")
def list_vehicles(vehicle_type: str = None, limit: int = 50, offset: int = 0,
                  db: Session = Depends(get_db)):
    return vehicle_service.list_vehicles(db, vehicle_type=vehicle_type, limit=limit, offset=offset)


@router.post("/vehicles", response_model=VehicleActionResponse, summary="UC4 — Register a new vehicle")
def register_vehicle(body: VehicleCreate, db: Session = Depends(get_db)):
    """Register an employee or visitor vehicle by plate number."""
    try:
        vehicle_service.register_vehicle(
            db, plate_number=body.plate_number, owner_name=body.owner_name,
            vehicle_type=body.vehicle_type, employee_id=body.employee_id,
            notes=body.notes,
        )
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "registered", "plate": body.plate_number}


@router.delete("/vehicles/{plate}", response_model=VehicleActionResponse, summary="UC4 — Remove a vehicle")
def remove_vehicle(plate: str, db: Session = Depends(get_db)):
    try:
        vehicle_service.remove_vehicle(db, plate)
        db.commit()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"status": "removed", "plate": plate}


@router.get("/vehicles/lookup/{plate}", response_model=VehicleLookupResponse, summary="UC4 — Look up a plate number")
def lookup_vehicle(plate: str, db: Session = Depends(get_db)):
    vehicle = vehicle_service.lookup_vehicle(db, plate)
    if not vehicle:
        return {"plate": plate, "status": "unknown", "registered": False}
    return {"plate": plate, "status": "known", "registered": True,
            "owner": vehicle.owner_name, "type": vehicle.vehicle_type}
