# app/models/vehicle.py
"""
Registered vehicles table (UC4).
Stores employee and visitor vehicles by plate number.
Used by entry_exit_service to identify known vs unknown vehicles.
Data source is temporary (local DB) — will be replaced by external HR API.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean
from app.database import Base


class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plate_number = Column(String(50), unique=True, nullable=False, index=True)
    owner_name = Column(String(200), nullable=False)
    title = Column(String(50), nullable=False)
    vehicle_type = Column(String(50), nullable=False)  # employee | visitor
    employee_id = Column(String(100))
    is_employee = Column(Boolean, default=False, nullable=False)
    phone = Column(String(50))
    email = Column(String(255))
    is_registered = Column(Boolean, default=False, nullable=False)
    registered_at = Column(DateTime)
    notes = Column(Text)

    # Set by parking_session_service.bind_slot when VA reports the vehicle
    # parked in a slot, cleared by unbind_slot when it leaves. Matches the
    # varchar slot_id used in parking_sessions / parking_slots / slot_status.
    current_slot_id = Column(String(50), index=True)

    # "Where is this car right now?" — written by VA on every track
    # confirmation (across cameras, parked or moving) and kept in sync by
    # bind_slot / close_session. Lets the Gateway answer presence without
    # JOINing parking_sessions, which only has floor info while bound.
    floor = Column(String(50))
    floor_id = Column(Integer)

    def __repr__(self):
        return f"<Vehicle {self.plate_number} owner={self.owner_name} type={self.vehicle_type}>"
