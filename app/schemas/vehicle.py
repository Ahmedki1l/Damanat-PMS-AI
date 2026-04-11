# app/schemas/vehicle.py
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional


class VehicleCreate(BaseModel):
    plate_number: str
    owner_name: str
    title: Optional[str] = None
    vehicle_type: str        # employee | visitor
    employee_id: Optional[str] = None
    is_employee: Optional[bool] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None


class VehicleUpdate(BaseModel):
    owner_name: Optional[str] = None
    title: Optional[str] = None
    vehicle_type: Optional[str] = None
    employee_id: Optional[str] = None
    is_employee: Optional[bool] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    notes: Optional[str] = None


class VehicleResponse(BaseModel):
    id: int
    plate_number: str
    owner_name: str
    title: str
    vehicle_type: str
    employee_id: Optional[str]
    is_employee: bool
    phone: Optional[str]
    email: Optional[str]
    is_registered: bool
    registered_at: Optional[datetime]
    notes: Optional[str]

    model_config = ConfigDict(from_attributes=True)

