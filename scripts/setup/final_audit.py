import sys; sys.path.insert(0, '.')
from app.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    print('=== camera_events summary (count + CDN coverage) ===')
    rows = conn.execute(text("""
        SELECT camera_id, event_type,
               COUNT(*) as total,
               SUM(CASE WHEN snapshot_path LIKE 'https%' THEN 1 ELSE 0 END) as with_cdn
        FROM camera_events
        GROUP BY camera_id, event_type
        ORDER BY camera_id, event_type
    """)).fetchall()
    all_ok = True
    for r in rows:
        status = 'OK' if r[2] == r[3] else f'MISSING {r[2]-r[3]}'
        if r[2] != r[3]:
            all_ok = False
        print(f'  {r[0]:12s} {r[1]:25s} total={r[2]:3d}  cdn={r[3]:3d}  [{status}]')
    print(f'\n  Overall: {"ALL HAVE CDN URLS" if all_ok else "SOME MISSING CDN URLS"}')

    print()
    print('=== entry_exit_log ===')
    rows2 = conn.execute(text(
        "SELECT id, plate_number, gate, event_time, CASE WHEN snapshot_path IS NOT NULL THEN 'YES' ELSE 'NO' END FROM entry_exit_log ORDER BY event_time"
    )).fetchall()
    for r in rows2:
        print(f'  id={r[0]} plate={r[1]:12s} gate={r[2]} time={r[3]} snap={r[4]}')
