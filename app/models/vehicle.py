# app/models/vehicle.py
"""
🔜 Phase 2: Registered vehicles table (UC4).
Stores employee and visitor vehicles by plate number.
Used by entry_exit_service to identify known vs unknown vehicles.
"""

from sqlalchemy import Column, Integer, String, DateTime, Text
from app.database import Base


class Vehicle(Base):
    __tablename__ = "vehicles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plate_number = Column(String(50), unique=True, nullable=False, index=True)
    owner_name = Column(String(200), nullable=False)
    vehicle_type = Column(String(50), nullable=False)  # employee | visitor
    employee_id = Column(String(100))
    is_registered = Column(Integer, default=1, nullable=False)
    registered_at = Column(DateTime)
    notes = Column(Text)

    def __repr__(self):
        return f"<Vehicle {self.plate_number} owner={self.owner_name} type={self.vehicle_type}>"
