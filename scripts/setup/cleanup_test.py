import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text("DELETE FROM camera_events WHERE id = 162"))
    conn.execute(text("DELETE FROM entry_exit_log WHERE plate_number = 'TST-1234'"))
    conn.execute(text("DELETE FROM alerts WHERE description LIKE '%TST-1234%'"))
    conn.commit()
    print("Test record cleaned up")
