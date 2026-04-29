import sqlalchemy
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.models.entry_exit_log import EntryExitLog
from app.models.vehicle import Vehicle

engine = sqlalchemy.create_engine(settings.db_url)
Session = sessionmaker(bind=engine)
session = Session()

last_log = session.query(EntryExitLog).order_by(EntryExitLog.id.desc()).first()
if last_log:
    print(f"Last Log ID: {last_log.id}")
    print(f"Plate: {last_log.plate_number}")
    print(f"Gate: {last_log.gate}")
    print(f"Vehicle ID: {last_log.vehicle_id}")
    
    if last_log.vehicle_id:
        v = session.query(Vehicle).get(last_log.vehicle_id)
        print(f"Vehicle Plate in DB: {v.plate_number if v else 'NOT FOUND'}")
else:
    print("No logs found.")
