import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print('=== camera_events by type/camera ===')
    rows = conn.execute(text(
        'SELECT camera_id, event_type, COUNT(*) as c FROM camera_events '
        'GROUP BY camera_id, event_type ORDER BY camera_id, event_type'
    )).fetchall()
    for r in rows:
        print(f'  {r[0]:15s} {r[1]:30s} count={r[2]}')

    print()
    print('=== entry_exit_log ===')
    rows2 = conn.execute(text(
        'SELECT id, plate_number, gate, event_time FROM entry_exit_log ORDER BY id'
    )).fetchall()
    for r in rows2:
        print(f'  id={r[0]} plate={r[1]} gate={r[2]} time={r[3]}')

