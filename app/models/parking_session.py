"""
Historical parking session read model.

Each row represents one stay inside the parking facility.
Raw gate events continue to live in entry_exit_log.
"""

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Text, Boolean

from app.database import Base


class ParkingSession(Base):
    __tablename__ = "parking_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plate_number = Column(String(50), nullable=False, index=True)
    vehicle_id = Column(Integer, ForeignKey("vehicles.id", ondelete="NO ACTION"))
    vehicle_type = Column(String(50))
    is_employee = Column(Boolean, default=False, nullable=False)

    entry_time = Column(DateTime, nullable=False, index=True)
    exit_time = Column(DateTime, index=True)
    duration_seconds = Column(Integer)

    entry_camera_id = Column(String(50), nullable=False)
    exit_camera_id = Column(String(50))

    entry_snapshot_path = Column(Text)
    exit_snapshot_path = Column(Text)

    floor = Column(String(50))
    zone_id = Column(String(100), index=True)
    zone_name = Column(String(100))
    slot_number = Column(String(100))
    parked_at = Column(DateTime)
    slot_left_at = Column(DateTime)
    slot_camera_id = Column(String(50))
    slot_snapshot_path = Column(Text)

    status = Column(String(20), nullable=False, default="open", index=True)
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)

    def __repr__(self):
        return f"<ParkingSession {self.id} plate={self.plate_number} status={self.status}>"
