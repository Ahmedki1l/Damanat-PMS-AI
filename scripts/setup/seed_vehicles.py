#!/usr/bin/env python3
"""
Seed the vehicles table with demo data: 10 employees + 5 visitors.
Plate format matches Egyptian private plates as read by Hikvision ANPR.
Usage: python scripts/setup/seed_vehicles.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from datetime import datetime
from app.database import SessionLocal
from app.models.vehicle import Vehicle

DEMO_VEHICLES = [
    # Employees (Egyptian private plate format: 3 Arabic letters + 4 digits)
    {"plate_number": "ا ب ج 1234", "owner_name": "Ahmed Hassan", "vehicle_type": "employee", "employee_id": "EMP-001"},
    {"plate_number": "س م ر 5678", "owner_name": "Sara Mohamed", "vehicle_type": "employee", "employee_id": "EMP-002"},
    {"plate_number": "ع م ل 9012", "owner_name": "Omar Khalil", "vehicle_type": "employee", "employee_id": "EMP-003"},
    {"plate_number": "ف ط ن 3456", "owner_name": "Fatima Ali", "vehicle_type": "employee", "employee_id": "EMP-004"},
    {"plate_number": "ي و س 7890", "owner_name": "Youssef Nader", "vehicle_type": "employee", "employee_id": "EMP-005"},
    {"plate_number": "ن و ر 2345", "owner_name": "Nour Ibrahim", "vehicle_type": "employee", "employee_id": "EMP-006"},
    {"plate_number": "خ ل د 6789", "owner_name": "Khaled Mansour", "vehicle_type": "employee", "employee_id": "EMP-007"},
    {"plate_number": "ل ي ل 1357", "owner_name": "Layla Samir", "vehicle_type": "employee", "employee_id": "EMP-008"},
    {"plate_number": "ت م ر 2468", "owner_name": "Tamer Reda", "vehicle_type": "employee", "employee_id": "EMP-009"},
    {"plate_number": "م ر م 3579", "owner_name": "Mariam Fathy", "vehicle_type": "employee", "employee_id": "EMP-010"},
    # Visitors
    {"plate_number": "د ل ف 4001", "owner_name": "Delivery Co.", "vehicle_type": "visitor", "notes": "Regular delivery van"},
    {"plate_number": "ت ق ن 4002", "owner_name": "IT Support Ltd.", "vehicle_type": "visitor", "notes": "Weekly maintenance"},
    {"plate_number": "ن ظ ف 4003", "owner_name": "Cleaning Services", "vehicle_type": "visitor", "notes": "Daily cleaning crew"},
    {"plate_number": "ع م ل 8010", "owner_name": "Client A", "vehicle_type": "visitor", "notes": "Temporary parking pass"},
    {"plate_number": "ز ي ر 8020", "owner_name": "Client B", "vehicle_type": "visitor", "notes": "Temporary parking pass"},
]


def seed():
    db = SessionLocal()
    added = 0
    skipped = 0
    try:
        for v in DEMO_VEHICLES:
            existing = db.query(Vehicle).filter(Vehicle.plate_number == v["plate_number"]).first()
            if existing:
                skipped += 1
                continue
            db.add(Vehicle(
                plate_number=v["plate_number"],
                owner_name=v["owner_name"],
                vehicle_type=v["vehicle_type"],
                employee_id=v.get("employee_id"),
                notes=v.get("notes"),
                is_registered=True,
                registered_at=datetime.utcnow(),
            ))
            added += 1
        db.commit()
        print(f"Seeded {added} vehicles ({skipped} already existed).")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
