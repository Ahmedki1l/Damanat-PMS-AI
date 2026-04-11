# app/schemas/camera_event.py
from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional


class CameraEventOut(BaseModel):
    id: int
    camera_id: str
    device_serial: str
    channel_id: Optional[int]
    event_type: str
    event_state: Optional[str]
    event_description: Optional[str]
    detection_target: Optional[str]
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: Optional[datetime]
    snapshot_path: Optional[str]
    created_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)

