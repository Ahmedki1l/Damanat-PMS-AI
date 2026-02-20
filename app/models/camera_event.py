# app/models/camera_event.py
"""
Raw camera event log table.
Stores every event received from all cameras, regardless of type.
Used for audit trail, debugging, and event replay.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text
from app.database import Base


class CameraEvent(Base):
    __tablename__ = "camera_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    camera_id = Column(String(50), nullable=False, index=True)
    device_serial = Column(String(100))
    channel_id = Column(Integer)
    event_type = Column(String(100), nullable=False, index=True)
    detection_target = Column(String(50))
    region_id = Column(String(100))
    channel_name = Column(String(100))
    trigger_time = Column(DateTime)
    raw_payload = Column(Text)
    created_at = Column(DateTime, nullable=False, index=True)

    def __repr__(self):
        return f"<CameraEvent {self.id} type={self.event_type} cam={self.camera_id}>"
