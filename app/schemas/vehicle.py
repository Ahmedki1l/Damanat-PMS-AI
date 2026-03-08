# app/schemas/vehicle.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class VehicleCreate(BaseModel):
    plate_number: str
    owner_name: str
    vehicle_type: str        # employee | visitor
    employee_id: Optional[str] = None
    notes: Optional[str] = None


class VehicleResponse(BaseModel):
    id: int
    plate_number: str
    owner_name: str
    vehicle_type: str
    employee_id: Optional[str]
    is_registered: bool
    registered_at: Optional[datetime]
    notes: Optional[str]

    class Config:
        from_attributes = True
