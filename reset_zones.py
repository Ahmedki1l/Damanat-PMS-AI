# reset_zones.py
from app.database import SessionLocal
from app.models.zone_occupancy import ZoneOccupancy

# Define your desired capacities here
ZONE_CAPACITIES = {
    "GARAGE-TOTAL": 100,
    "B1-PARKING": 50,
    "B2-PARKING": 50
}

db = SessionLocal()
try:
    # 1. Update existing zones
    zones = db.query(ZoneOccupancy).all()
    for zone in zones:
        new_cap = ZONE_CAPACITIES.get(zone.zone_id, 50)
        print(f"Setting {zone.zone_id}: Count {zone.current_count}->0, Capacity {zone.max_capacity}->{new_cap}")
        zone.current_count = 0
        zone.max_capacity = new_cap
    
    # 2. Ensure total zone exists if it was deleted
    if not any(z.zone_id == "GARAGE-TOTAL" for z in zones):
        print("Creating GARAGE-TOTAL zone...")
        db.add(ZoneOccupancy(zone_id="GARAGE-TOTAL", current_count=0, max_capacity=100))

    db.commit()
    print("✅ All zones reset and capacities updated successfully.")
finally:
    db.close()
