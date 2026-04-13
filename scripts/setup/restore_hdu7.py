"""Restore the HDU-7 entry that was incorrectly removed. 
The 065039 image is the cleaner shot — use that as the snapshot."""
import sys, os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.database import SessionLocal
from app.models.camera_event import CameraEvent
from app.models.entry_exit_log import EntryExitLog
from app.utils.spaces_client import upload_image
from sqlalchemy import text

IMAGE_PATH = os.path.join("detection_images", "part_ANPR_CAM-ENTRY_20260311_065039_716595.jpg")
PLATE      = "HDU-7"
EVENT_TIME = datetime(2026, 3, 11, 6, 50, 29)
CAMERA_ID  = "CAM-ENTRY"

db = SessionLocal()
try:
    # Upload best image to Spaces
    print(f"Uploading {IMAGE_PATH} ...")
    with open(IMAGE_PATH, "rb") as f:
        data = f.read()
    cdn_url = upload_image(data, os.path.basename(IMAGE_PATH))
    if not cdn_url:
        print("ERROR: Upload failed")
        raise SystemExit(1)
    print(f"CDN: {cdn_url}")

    # camera_event
    ev = CameraEvent(
        camera_id         = CAMERA_ID,
        device_serial     = "DS-2CD2T47G2-ENTRY",
        channel_id        = 1,
        event_type        = "ANPR",
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

    # entry_exit_log
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

