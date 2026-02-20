# app/schemas/alert.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class AlertOut(BaseModel):
    id: int
    alert_type: str
    camera_id: str
    zone_id: Optional[str]
    event_type: Optional[str]
    description: Optional[str]
    is_resolved: int
    triggered_at: datetime
    resolved_at: Optional[datetime]

    class Config:
        from_attributes = True
