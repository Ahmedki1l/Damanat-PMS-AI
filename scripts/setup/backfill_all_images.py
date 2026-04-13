"""
Backfill script: uploads all local detection_images/* to DO Spaces and creates/updates
camera_events records for every image that has no matching DB entry.

Strategy:
- Parse filename: {prefix}_{event_type}_{camera_id}_{YYYYMMDD}_{HHMMSS}_{usec}.jpg
- Match existing camera_events by (camera_id, event_type, trigger_time ±30s)
- If no match → upload to Spaces, insert camera_event
- If match but snapshot_path is NULL → upload to Spaces, update record
- If match and snapshot_path already set → skip (already done)

Preference: snap_* over part_* for same event (full-frame, higher quality).

Run from project root:
    python scripts/setup/backfill_all_images.py
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import SessionLocal
from app.models.camera_event import CameraEvent
from app.utils.spaces_client import upload_image
from app.config import settings
from sqlalchemy import text

DETECTION_DIR = "detection_images"

# Known camera device serials (best effort)
CAMERA_SERIALS = {
    "CAM-02":    "DS-2CD2T47G2-L-20",
    "CAM-04":    "DS-2CD2T47G2-L-04",
    "CAM-05":    "DS-2CD2T47G2-L-05",
    "CAM-06":    "DS-2CD2T47G2-L-06",
    "CAM-ENTRY": "DS-2CD2T47G2-ENTRY",
    "CAM-EXIT":  "DS-2CD2T47G2-EXIT",
}

# Detection target per event type
EVENT_TARGET = {
    "fielddetection":  "vehicle",
    "linedetection":   "vehicle",
    "regionEntrance":  "vehicle",
    "regionExiting":   "vehicle",
    "AccessControllerEvent": "vehicle",
    "vehicleMatchResult":    "vehicle",
    "ANPR":            "vehicle",
}


def parse_filename(filename: str):
    """
    Parse detection image filename into components.
    Formats:
      snap_fielddetection_CAM-04_20260310_125838_032656.jpg
      part_fielddetection_CAM-04_20260310_125838_021872.jpg
      part_ANPR_CAM-ENTRY_20260311_055920_544990.jpg
    Returns dict or None if unparsable.
    """
    stem = filename.rstrip(".jpg").rstrip(".JPG")
    # Pattern: {prefix}_{event_type}_{camera_id}_{date}_{time}_{usec}
    # camera_id may contain hyphens, e.g. CAM-04, CAM-ENTRY
    m = re.match(
        r"^(snap|part)_([a-zA-Z]+)_(CAM-[A-Z0-9]+)_(\d{8})_(\d{6})_(\d+)$",
        stem,
    )
    if not m:
        return None

    prefix, event_type, camera_id, date_str, time_str, usec = m.groups()

    # Normalize ANPR → AccessControllerEvent (same pipeline)
    if event_type.upper() == "ANPR":
        event_type = "AccessControllerEvent"

    try:
        dt = datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")
    except ValueError:
        return None

    return {
        "prefix":     prefix,       # "snap" or "part"
        "event_type": event_type,
        "camera_id":  camera_id,
        "trigger_time": dt,
        "filename":   filename,
        "filepath":   os.path.join(DETECTION_DIR, filename),
    }


def load_all_images():
    """Return list of parsed image dicts sorted by camera_id + trigger_time."""
    images = []
    for fname in os.listdir(DETECTION_DIR):
        if not fname.endswith(".jpg"):
            continue
        parsed = parse_filename(fname)
        if parsed:
            images.append(parsed)
        else:
            print(f"  [SKIP] Could not parse filename: {fname}")

    # Sort: snap before part for same event (snap preferred for DB)
    images.sort(key=lambda x: (x["camera_id"], x["event_type"], x["trigger_time"], x["prefix"]))
    return images


def upload_to_spaces(filepath: str, filename: str) -> str | None:
    """Upload image file to DO Spaces. Return CDN URL or None."""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        return upload_image(data, filename)
    except Exception as e:
        print(f"  [ERROR] Upload failed for {filename}: {e}")
        return None


def find_existing_event(db, camera_id: str, event_type: str, trigger_time: datetime):
    """
    Find a camera_event with matching camera_id + event_type within ±30s of trigger_time.
    Returns the ORM object or None.
    """
    window_start = trigger_time - timedelta(seconds=30)
    window_end   = trigger_time + timedelta(seconds=30)
    return (
        db.query(CameraEvent)
        .filter(
            CameraEvent.camera_id   == camera_id,
            CameraEvent.event_type  == event_type,
            CameraEvent.trigger_time >= window_start,
            CameraEvent.trigger_time <= window_end,
        )
        .first()
    )


def main():
    print(f"Storage mode: {settings.STORAGE_MODE}")
    if settings.STORAGE_MODE != "spaces":
        print("ERROR: STORAGE_MODE is not 'spaces' — set it in .env and retry.")
        sys.exit(1)

    images = load_all_images()
    print(f"\nFound {len(images)} parseable images in {DETECTION_DIR}/\n")

    # Deduplicate: for each (camera_id, event_type, trigger_time) keep snap_ over part_
    # Group by rounded 30-second bucket
    best: dict[tuple, dict] = {}
    for img in images:
        bucket_sec = int(img["trigger_time"].timestamp() // 30) * 30
        key = (img["camera_id"], img["event_type"], bucket_sec)
        existing = best.get(key)
        if existing is None:
            best[key] = img
        elif img["prefix"] == "snap" and existing["prefix"] == "part":
            best[key] = img  # prefer snap over part

    unique_images = sorted(best.values(), key=lambda x: (x["camera_id"], x["trigger_time"]))
    print(f"Unique events to process (after dedup): {len(unique_images)}\n")

    db = SessionLocal()
    inserted = 0
    updated  = 0
    skipped  = 0

    try:
        for img in unique_images:
            cam    = img["camera_id"]
            etype  = img["event_type"]
            tstamp = img["trigger_time"]
            fpath  = img["filepath"]
            fname  = img["filename"]

            existing = find_existing_event(db, cam, etype, tstamp)

            if existing and existing.snapshot_path and existing.snapshot_path.startswith("http"):
                print(f"  [SKIP] {cam} {etype} {tstamp} — already has CDN URL")
                skipped += 1
                continue

            # Upload to Spaces
            cdn_url = upload_to_spaces(fpath, fname)
            if not cdn_url:
                print(f"  [FAIL] Could not upload {fname}")
                continue

            if existing:
                # Update existing record with snapshot_path
                existing.snapshot_path = cdn_url
                print(f"  [UPDATE] id={existing.id} {cam} {etype} {tstamp} -> {cdn_url[:70]}...")
                updated += 1
            else:
                # Create new camera_event
                cam_config = settings.CAMERAS.get(cam, {})
                ev = CameraEvent(
                    camera_id         = cam,
                    device_serial     = CAMERA_SERIALS.get(cam, "unknown"),
                    channel_id        = 1,
                    event_type        = etype,
                    event_state       = "active",
                    event_description = f"{etype} detected",
                    detection_target  = EVENT_TARGET.get(etype, "vehicle"),
                    region_id         = cam_config.get("gate") or "1",
                    channel_name      = cam_config.get("name") or cam,
                    trigger_time      = tstamp,
                    snapshot_path     = cdn_url,
                    raw_payload       = None,
                    is_test           = False,
                    created_at        = datetime.now(UTC),
                )
                db.add(ev)
                db.flush()
                print(f"  [INSERT] id={ev.id} {cam} {etype} {tstamp} -> {cdn_url[:70]}...")
                inserted += 1

        db.commit()
        print(f"\nDone. inserted={inserted}  updated={updated}  skipped={skipped}")

    except Exception as e:
        db.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()

