# app/schemas/alert.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class AlertOut(BaseModel):
    id: int
    alert_type: str
    camera_id: str
    zone_id: Optional[str]
    zone_name: Optional[str]
    region_id: Optional[int]
    event_type: Optional[str]
    description: Optional[str]
    is_resolved: bool
    triggered_at: datetime
    resolved_at: Optional[datetime]

    class Config:
        from_attributes = True