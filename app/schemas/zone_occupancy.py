# app/schemas/zone_occupancy.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class ZoneOccupancyOut(BaseModel):
    id: int
    zone_id: str
    camera_id: str
    current_count: int
    max_capacity: int
    occupancy_percent: Optional[float] = None
    is_full: Optional[bool] = None
    last_updated: Optional[datetime]

    class Config:
        from_attributes = True


class ZoneCapacityUpdate(BaseModel):
    max_capacity: int
