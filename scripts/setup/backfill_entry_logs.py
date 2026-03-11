"""
Backfill two real vehicle entry events that were captured locally
but never uploaded to Spaces or saved to the database.
"""
import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import SessionLocal
from app.models.entry_exit_log import EntryExitLog
from app.services import vehicle_service
from app.utils.spaces_client import upload_image
from app.utils.logger import get_logger

logger = get_logger(__name__)

ENTRIES = [
    {
        "local_file": "detection_images/part_ANPR_CAM-ENTRY_20260311_055920_544990.jpg",
        "plate":      "9444HUD",
        "event_time": datetime(2026, 3, 11, 5, 58, 55),   # 08:58:55 local = 05:58:55 UTC
        "camera_id":  "CAM-ENTRY",
    },
    {
        "local_file": "detection_images/part_ANPR_CAM-ENTRY_20260311_060744_040706.jpg",
        "plate":      "7651XHD",
        "event_time": datetime(2026, 3, 11, 6, 7, 37),    # 09:07:37 local = 06:07:37 UTC
        "camera_id":  "CAM-ENTRY",
    },
]


def run():
    db = SessionLocal()
    try:
        for entry in ENTRIES:
            plate    = entry["plate"]
            cam_id   = entry["camera_id"]
            evt_time = entry["event_time"]
            filepath = entry["local_file"]

            print(f"\n--- Processing {plate} ---")

            # 1. Upload image to Spaces
            filename = os.path.basename(filepath)
            with open(filepath, "rb") as f:
                image_bytes = f.read()

            cdn_url = upload_image(image_bytes, filename)
            if not cdn_url:
                print(f"  ERROR: Upload failed for {filename}, skipping.")
                continue
            print(f"  Uploaded: {cdn_url}")

            # 2. Resolve vehicle identity
            vehicle = vehicle_service.lookup_vehicle(db, plate)
            vehicle_type = vehicle.vehicle_type if vehicle else "unknown"
            print(f"  Vehicle: {vehicle_type} | Registered: {vehicle is not None}")

            # 3. Insert entry_exit_log record (entry only — cars still inside)
            log = EntryExitLog(
                plate_number   = plate,
                vehicle_id     = vehicle.id if vehicle else None,
                vehicle_type   = vehicle_type,
                gate           = "entry",
                camera_id      = cam_id,
                event_time     = evt_time,
                snapshot_path  = cdn_url,
                is_test        = False,
                created_at     = datetime.utcnow(),
            )
            db.add(log)
            db.flush()
            print(f"  Created entry_exit_log id={log.id}  plate={plate}  time={evt_time}")

        db.commit()
        print("\nDone — both entries committed to database.")

    except Exception as e:
        db.rollback()
        print(f"ERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
