"""Remove entry_exit_log and camera_event records with invalid/partial plates."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from app.database import engine
from sqlalchemy import text

INVALID_PLATES = ('UNKNOWN', 'HDU-7')

with engine.connect() as conn:
    for plate in INVALID_PLATES:
        r1 = conn.execute(text(f"DELETE FROM alerts WHERE description LIKE '%{plate}%'"))
        r2 = conn.execute(text(f"DELETE FROM entry_exit_log WHERE plate_number = '{plate}'"))
        r3 = conn.execute(text(
            f"DELETE FROM camera_events WHERE camera_id='CAM-ENTRY' "
            f"AND raw_payload LIKE '%{plate.replace('-','')}%' OR raw_payload LIKE '%unknown%'"
        ))
        print(f"Plate {plate}: removed alerts={r1.rowcount} entries={r2.rowcount}")
    conn.commit()

    print("\nCurrent entry_exit_log:")
    rows = conn.execute(text(
        "SELECT id, plate_number, gate, event_time FROM entry_exit_log ORDER BY event_time"
    )).fetchall()
    for r in rows:
        print(f"  id={r[0]}  plate={r[1]:12s}  gate={r[2]}  time={r[3]}")

