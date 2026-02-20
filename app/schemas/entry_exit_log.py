# app/schemas/entry_exit_log.py  🔜 Phase 2
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class EntryExitLogOut(BaseModel):
    id: int
    plate_number: str
    vehicle_type: Optional[str]
    gate: str
    camera_id: str
    event_time: datetime
    parking_duration: Optional[int]   # seconds
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class DailyStatsOut(BaseModel):
    date: str
    total_vehicles: int
    avg_parking_minutes: float
