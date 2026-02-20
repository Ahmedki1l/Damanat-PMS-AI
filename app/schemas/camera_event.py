# app/schemas/camera_event.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class CameraEventOut(BaseModel):
    id: int
    camera_id: str
    device_serial: str
    channel_id: Optional[int]
    event_type: str
    detection_target: Optional[str]
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: Optional[datetime]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True
