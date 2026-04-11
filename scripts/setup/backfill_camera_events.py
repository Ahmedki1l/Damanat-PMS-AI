"""
Backfill camera_events for the two real ANPR entries that were
captured locally but never written to the events table.
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.database import SessionLocal
from app.models.camera_event import CameraEvent
from sqlalchemy import text

ENTRIES = [
    {
        "plate":         "HUD-9444",
        "event_time":    datetime(2026, 3, 11, 5, 58, 55),
        "camera_id":     "CAM-ENTRY",
        "device_serial": "DS-2CD7A26G0/P-IZHS",
        "cdn_url": "https://cognerax-learn.sfo3.cdn.digitaloceanspaces.com/detection_images/part_ANPR_CAM-ENTRY_20260311_055920_544990.jpg",
    },
    {
        "plate":         "XHD-7651",
        "event_time":    datetime(2026, 3, 11, 6, 7, 37),
        "camera_id":     "CAM-ENTRY",
        "device_serial": "DS-2CD7A26G0/P-IZHS",
        "cdn_url": "https://cognerax-learn.sfo3.cdn.digitaloceanspaces.com/detection_images/part_ANPR_CAM-ENTRY_20260311_060744_040706.jpg",
    },
]

db = SessionLocal()
try:
    for e in ENTRIES:
        ev = CameraEvent(
            camera_id         = e["camera_id"],
            device_serial     = e["device_serial"],
            channel_id        = 1,
            event_type        = "AccessControllerEvent",
            event_state       = "active",
            event_description = "Vehicle entry detected",
            detection_target  = "vehicle",
            region_id         = "entry",
            channel_name      = "ENTRY-GATE",
            trigger_time      = e["event_time"],
            snapshot_path     = e["cdn_url"],
            raw_payload       = None,
            is_test           = False,
            created_at        = datetime.now(UTC),
        )
        db.add(ev)
        db.flush()
        print(f"Created camera_event id={ev.id}  plate={e['plate']}  time={e['event_time']}")
        print(f"  snapshot={e['cdn_url']}")

    db.commit()
    print("\nDone.")
except Exception as ex:
    db.rollback()
    print(f"ERROR: {ex}")
    raise
finally:
    db.close()

