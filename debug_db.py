# debug_db.py
from app.database import SessionLocal
from app.models.zone_occupancy import ZoneOccupancy

db = SessionLocal()
try:
    print("--- Listing all ZoneOccupancy records ---")
    zones = db.query(ZoneOccupancy).all()
    if not zones:
        print("No zones found in DB.")
    for zone in zones:
        # Use repr to see exact strings (hidden spaces/chars)
        print(f"ID={zone.id} | zone_id={repr(zone.zone_id)} | count={zone.current_count} | max={zone.max_capacity} | last_updated={zone.last_updated}")
    print("-----------------------------------------")
finally:
    db.close()
