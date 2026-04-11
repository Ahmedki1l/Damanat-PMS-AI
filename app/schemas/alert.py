# app/schemas/alert.py
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional


class AlertOut(BaseModel):
    id: int
    alert_type: str
    camera_id: str
    zone_id: Optional[str]
    zone_name: Optional[str]
    region_id: Optional[int]
    slot_number: Optional[str] = None  # real-life parking bay number
    plate_number: Optional[str] = None
    severity: str
    location_display: Optional[str] = None
    event_type: Optional[str]
    description: Optional[str]
    is_resolved: bool
    triggered_at: datetime
    resolved_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)

