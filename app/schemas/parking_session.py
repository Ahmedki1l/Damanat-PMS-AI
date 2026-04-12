from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class ParkingSessionBindRequest(BaseModel):
    plate_number: str
    slot_id: str
    slot_number: str
    zone_id: str
    zone_name: Optional[str] = None
    floor: Optional[str] = None
    camera_id: str
    parked_at: Optional[datetime] = None
    snapshot_path: Optional[str] = None


class ParkingSessionUnbindRequest(BaseModel):
    plate_number: str
    camera_id: str
    slot_id: Optional[str] = None
    slot_number: Optional[str] = None
    left_at: Optional[datetime] = None
    snapshot_path: Optional[str] = None


class ParkingSessionActionResponse(BaseModel):
    session_id: int
    plate_number: str
    status: str
    slot_id: Optional[str] = None
    zone_id: Optional[str] = None
    zone_name: Optional[str] = None
    floor: Optional[str] = None
    slot_number: Optional[str] = None

