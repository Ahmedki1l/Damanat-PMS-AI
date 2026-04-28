# app/schemas/vehicle.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class VehicleCreate(BaseModel):
    plate_number: str
    owner_name: str
    title: str
    vehicle_type: str        # employee | visitor
    employee_id: Optional[str] = None
    is_employee: Optional[bool] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None

class VehicleUpdate(BaseModel):
    plate_number: Optional[str] = None
    owner_name: Optional[str] = None
    title: Optional[str] = None
    vehicle_type: Optional[str] = None
    employee_id: Optional[str] = None
    is_employee: Optional[bool] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None
    is_registered: Optional[bool] = None

class VehicleKPI(BaseModel):
    total_vehicles: int

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
