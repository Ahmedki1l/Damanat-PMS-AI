import sys; sys.path.insert(0, '.')
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print("Events with no CDN URL (excluding 'duration' events):")
    rows = conn.execute(text(
        "SELECT id, camera_id, event_type, trigger_time, snapshot_path "
        "FROM camera_events "
        "WHERE (snapshot_path IS NULL OR snapshot_path NOT LIKE 'https%') "
        "AND event_type != 'duration' "
        "ORDER BY camera_id, trigger_time"
    )).fetchall()
    for r in rows:
        print(f"  id={r[0]} cam={r[1]} type={r[2]} time={r[3]} snap={r[4]}")

    if not rows:
        print("  (none)")

    print()
    print("Duration events (no snapshots expected — metadata only):")
    rows2 = conn.execute(text(
        "SELECT id, camera_id, event_type, trigger_time "
        "FROM camera_events WHERE event_type = 'duration' ORDER BY camera_id, trigger_time"
    )).fetchall()
    for r in rows2:
        print(f"  id={r[0]} cam={r[1]} type={r[2]} time={r[3]}")
