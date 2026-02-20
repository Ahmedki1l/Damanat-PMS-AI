# app/models/alert.py
"""
Alerts table — stores all generated alerts (occupancy, violation, intrusion, unknown vehicle).
Used by violation_service, intrusion_service, occupancy_service, and entry_exit_service.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text
from app.database import Base


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_type = Column(String(50), nullable=False, index=True)
    camera_id = Column(String(50), nullable=False)
    zone_id = Column(String(100))
    event_type = Column(String(100))
    description = Column(Text)
    is_resolved = Column(Integer, default=0, nullable=False)
    triggered_at = Column(DateTime, nullable=False, index=True)
    resolved_at = Column(DateTime)

    def __repr__(self):
        return f"<Alert {self.id} type={self.alert_type} resolved={self.is_resolved}>"
