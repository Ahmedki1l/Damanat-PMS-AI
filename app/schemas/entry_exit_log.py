# app/schemas/entry_exit_log.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class EntryExitLogResponse(BaseModel):
    id: int
    plate_number: str
    vehicle_id: Optional[int]
    vehicle_type: Optional[str]
    gate: str
    camera_id: str
    event_time: datetime
    parking_duration: Optional[int]   # seconds
    matched_entry_id: Optional[int]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True
