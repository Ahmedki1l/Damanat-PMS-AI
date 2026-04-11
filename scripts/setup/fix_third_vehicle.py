"""
Fix the third ANPR entry: update plate from UNKNOWN-3 to ERD-7800.
Also dumps the raw vehicleMatchResult XML payloads so we can see why plate extraction failed.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.database import SessionLocal
from app.models.entry_exit_log import EntryExitLog
from sqlalchemy import text

db = SessionLocal()
try:
    # Fix plate
    entry = db.query(EntryExitLog).filter(EntryExitLog.plate_number == "UNKNOWN-3").first()
    if entry:
        old = entry.plate_number
        entry.plate_number = "ERD-7800"
        db.commit()
        print(f"Updated entry_exit_log id={entry.id}: {old} -> ERD-7800")
    else:
        print("No UNKNOWN-3 entry found")

    # Dump vehicleMatchResult raw payloads to understand plate extraction failure
    print("\n=== vehicleMatchResult raw_payload dump ===")
    rows = db.execute(text(
        "SELECT id, trigger_time, raw_payload FROM camera_events "
        "WHERE event_type = 'vehicleMatchResult' ORDER BY trigger_time"
    )).fetchall()
    for r in rows:
        payload = r[2] or "(null)"
        print(f"\n--- id={r[0]} time={r[1]} ---")
        print(payload[:2000])

finally:
    db.close()

