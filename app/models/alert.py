# app/models/alert.py
"""
Alerts table - stores all generated alerts (occupancy, violation, intrusion, unknown vehicle).
Used by violation_service, intrusion_service, occupancy_service, and entry_exit_service.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from app.database import Base
from app.zone_config import resolve_zone


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String(50), nullable=False, index=True)
    camera_id = Column(String(50), nullable=False)
    zone_id = Column(String(100))
    region_id = Column(Integer)
    event_type = Column(String(100))
    description = Column(Text)
    snapshot_path = Column(Text, nullable=True)   # CDN URL (Spaces) or local path
    is_test = Column(Boolean, default=False, nullable=False)  # True for simulated/test events
    is_resolved = Column(Boolean, default=False, nullable=False)
    triggered_at = Column(DateTime, nullable=False, index=True)
    resolved_at = Column(DateTime)

    @property
    def zone_name(self):
        if self.region_id is not None:
            resolved = resolve_zone(self.camera_id, self.region_id)
            if resolved:
                return resolved
        return self.zone_id

    def __repr__(self):
        return f"<Alert {self.id} type={self.alert_type} resolved={self.is_resolved}>"