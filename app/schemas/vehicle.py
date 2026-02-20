# app/schemas/vehicle.py  🔜 Phase 2
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class VehicleCreate(BaseModel):
    plate_number: str
    owner_name: str
    vehicle_type: str        # employee | visitor
    employee_id: Optional[str] = None
    notes: Optional[str] = None


class VehicleOut(BaseModel):
    id: int
    plate_number: str
    owner_name: str
    vehicle_type: str
    employee_id: Optional[str]
    is_registered: int
    registered_at: Optional[datetime]
    notes: Optional[str]

    class Config:
        from_attributes = True
