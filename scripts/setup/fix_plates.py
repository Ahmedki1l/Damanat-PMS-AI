import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text("UPDATE entry_exit_log SET plate_number = 'HUD-9444' WHERE plate_number = '9444HUD'"))
    conn.execute(text("UPDATE entry_exit_log SET plate_number = 'XHD-7651' WHERE plate_number = '7651XHD'"))
    conn.commit()

    rows = conn.execute(text("SELECT id, plate_number, gate, event_time FROM entry_exit_log ORDER BY id")).fetchall()
    for r in rows:
        print(f"id={r[0]}  plate={r[1]}  gate={r[2]}  time={r[3]}")

