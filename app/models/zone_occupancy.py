# app/models/zone_occupancy.py
"""
Zone occupancy state table (UC3).
Stores real-time vehicle count per parking zone.
Updated by occupancy_service on regionEntrance/regionExiting events.
"""

from sqlalchemy import Column, Integer, String, DateTime
from app.database import Base


class ZoneOccupancy(Base):
    __tablename__ = "zone_occupancy"

    id = Column(Integer, primary_key=True, autoincrement=True)
    zone_id = Column(String(100), unique=True, nullable=False, index=True)
    camera_id = Column(String(50), nullable=False)
    current_count = Column(Integer, default=0, nullable=False)
    max_capacity = Column(Integer, default=10, nullable=False)
    last_updated = Column(DateTime)

    def __repr__(self):
        return f"<ZoneOccupancy {self.zone_id} count={self.current_count}/{self.max_capacity}>"
