"""
Add the XRD-6663 ANPR entry that was captured at 06:39:15 but rolled back
due to the region_id schema bug (now fixed).

Image: detection_images/part_ANPR_CAM-ENTRY_20260311_063915_840576.jpg
Plate from server logs: 6663XRD -> XRD-6663
Time from filename:  2026-03-11 06:39:15 UTC
"""
import sys, os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.database import SessionLocal
from app.models.camera_event import CameraEvent
from app.models.entry_exit_log import EntryExitLog
from app.utils.spaces_client import upload_image
from sqlalchemy import text

IMAGE_PATH = os.path.join("detection_images", "part_ANPR_CAM-ENTRY_20260311_063915_840576.jpg")
PLATE       = "XRD-6663"
EVENT_TIME  = datetime(2026, 3, 11, 6, 39, 15)
CAMERA_ID   = "CAM-ENTRY"

db = SessionLocal()
try:
    # Check if already in DB
    existing_ev = db.execute(text(
        "SELECT id FROM camera_events WHERE camera_id='CAM-ENTRY' "
        "AND trigger_time BETWEEN '2026-03-11 06:38:45' AND '2026-03-11 06:39:45'"
    )).fetchone()

    existing_log = db.execute(text(
        "SELECT id FROM entry_exit_log WHERE plate_number='XRD-6663' "
        "AND event_time BETWEEN '2026-03-11 06:38:45' AND '2026-03-11 06:39:45'"
    )).fetchone()

    if existing_ev:
        print(f"camera_event already exists: id={existing_ev[0]}")
    if existing_log:
        print(f"entry_exit_log already exists: id={existing_log[0]}")
    if existing_ev and existing_log:
        print("Nothing to do.")
        db.close()
        raise SystemExit(0)

    # Upload image to Spaces
    print(f"Uploading {IMAGE_PATH} ...")
    with open(IMAGE_PATH, "rb") as f:
        image_bytes = f.read()

    filename = os.path.basename(IMAGE_PATH)
    cdn_url = upload_image(image_bytes, filename)
    if not cdn_url:
        print("ERROR: Spaces upload failed")
        raise SystemExit(1)
    print(f"CDN URL: {cdn_url}")

    # Create camera_event
    if not existing_ev:
        ev = CameraEvent(
            camera_id         = CAMERA_ID,
            device_serial     = "DS-2CD2T47G2-ENTRY",
            channel_id        = 1,
            event_type        = "AccessControllerEvent",
            event_state       = "active",
            event_description = "Vehicle entry detected",
            detection_target  = "vehicle",
            region_id         = "entry",
            channel_name      = "ANPR-1 Entry",
            trigger_time      = EVENT_TIME,
            snapshot_path     = cdn_url,
            raw_payload       = None,
            is_test           = False,
            created_at        = datetime.now(UTC),
        )
        db.add(ev)
        db.flush()
        print(f"Created camera_event id={ev.id}")

    # Create entry_exit_log
    if not existing_log:
        log = EntryExitLog(
            plate_number  = PLATE,
            camera_id     = CAMERA_ID,
            gate          = "entry",
            event_time    = EVENT_TIME,
            snapshot_path = cdn_url,
            is_test       = False,
            created_at    = datetime.now(UTC),
        )
        db.add(log)
        db.flush()
        print(f"Created entry_exit_log id={log.id}  plate={PLATE}")

    db.commit()
    print("Done.")

except SystemExit:
    raise
except Exception as e:
    db.rollback()
    print(f"ERROR: {e}")
    raise
finally:
    db.close()

