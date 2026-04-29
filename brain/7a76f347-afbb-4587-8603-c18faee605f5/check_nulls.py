import sqlalchemy
from sqlalchemy.orm import sessionmaker
from app.config import settings
from app.models.entry_exit_log import EntryExitLog

engine = sqlalchemy.create_engine(settings.db_url)
Session = sessionmaker(bind=engine)
session = Session()

logs = session.query(EntryExitLog).order_by(EntryExitLog.id.desc()).limit(20).all()
print(f"{'ID':<6} | {'Plate':<15} | {'Gate':<6} | {'VehicleID':<10}")
print("-" * 45)
for log in logs:
    vid = log.vehicle_id if log.vehicle_id else "NULL"
    print(f"{log.id:<6} | {log.plate_number:<15} | {log.gate:<6} | {vid:<10}")
