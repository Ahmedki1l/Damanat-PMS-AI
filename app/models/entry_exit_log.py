# app/models/entry_exit_log.py
"""
🔜 Phase 2: Entry/Exit log table (UC1 + UC2).
Records every vehicle entry and exit event from ANPR cameras.
Matched pairs are used to calculate parking duration.
"""

from sqlalchemy import Column, Integer, String, DateTime
from app.database import Base


class EntryExitLog(Base):
    __tablename__ = "entry_exit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plate_number = Column(String(50), nullable=False, index=True)
    vehicle_id = Column(Integer)             # FK to vehicles.id (nullable if unknown)
    vehicle_type = Column(String(50))        # employee | visitor | unknown
    gate = Column(String(20), nullable=False) # entry | exit
    camera_id = Column(String(50), nullable=False)
    event_time = Column(DateTime, nullable=False, index=True)
    parking_duration = Column(Integer)        # seconds (set on exit)
    matched_entry_id = Column(Integer)        # cross-reference to matching entry/exit
    created_at = Column(DateTime)

    def __repr__(self):
        return f"<EntryExitLog {self.id} plate={self.plate_number} gate={self.gate}>"
