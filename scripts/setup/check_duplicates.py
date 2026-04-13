import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print('Latest entry_exit_log entries:')
    rows = conn.execute(text(
        'SELECT id, plate_number, gate, event_time FROM entry_exit_log ORDER BY event_time DESC LIMIT 10'
    )).fetchall()
    for r in rows:
        print(f'  id={r[0]}  plate={r[1]:12s}  gate={r[2]}  time={r[3]}')

    print()
    print('Latest ANPR-related camera_events:')
    rows2 = conn.execute(text(
        "SELECT id, camera_id, event_type, trigger_time "
        "FROM camera_events "
        "WHERE event_type IN ('ANPR','vehicleMatchResult','AccessControllerEvent') "
        "ORDER BY trigger_time DESC LIMIT 10"
    )).fetchall()
    for r in rows2:
        print(f'  id={r[0]}  cam={r[1]}  type={r[2]:25s}  time={r[3]}')

