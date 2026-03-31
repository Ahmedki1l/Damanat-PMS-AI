from app.database import SessionLocal
from app.models.vehicle import Vehicle

def register():
    db = SessionLocal()
    try:
        v = db.query(Vehicle).filter(Vehicle.plate_number == 'TEST-OK').first()
        if not v:
            v = Vehicle(plate_number='TEST-OK', owner_name='Test User', vehicle_type='private')
            db.add(v)
            db.commit()
            print("Successfully registered TEST-OK for testing")
        else:
            print("TEST-OK is already registered")
    finally:
        db.close()

if __name__ == "__main__":
    register()
