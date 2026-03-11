"""
Fix vehicleMatchResult events (ids 74, 78, 79) which have no snapshot_path.
Match each to the nearest ANPR camera_event by timestamp, copy its CDN URL.
Also creates the missing entry_exit_log for the third ANPR vehicle.

Run from project root:
    python scripts/setup/fix_vehiclematch_snapshots.py
"""
import sys, os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.database import SessionLocal
from app.models.camera_event import CameraEvent
from app.models.entry_exit_log import EntryExitLog
from sqlalchemy import text

db = SessionLocal()

try:
    # ── Step 1: Patch snapshot_path on vehicleMatchResult events ─────────────
    # Find each vehicleMatchResult and match it to the closest AccessControllerEvent
    # from the same camera within a ±60s window.
    vm_events = (
        db.query(CameraEvent)
        .filter(
            CameraEvent.event_type == "vehicleMatchResult",
            CameraEvent.camera_id  == "CAM-ENTRY",
        )
        .order_by(CameraEvent.trigger_time)
        .all()
    )

    print(f"Found {len(vm_events)} vehicleMatchResult events:")
    for vm in vm_events:
        print(f"  id={vm.id}  time={vm.trigger_time}  snap={vm.snapshot_path or '(none)'}")

    print()

    for vm in vm_events:
        if vm.snapshot_path and vm.snapshot_path.startswith("http"):
            print(f"  [SKIP] id={vm.id} already has CDN URL")
            continue

        # Find the nearest AccessControllerEvent from CAM-ENTRY
        window_start = vm.trigger_time - timedelta(seconds=90)
        window_end   = vm.trigger_time + timedelta(seconds=90)
        nearest = (
            db.query(CameraEvent)
            .filter(
                CameraEvent.camera_id  == "CAM-ENTRY",
                CameraEvent.event_type == "AccessControllerEvent",
                CameraEvent.snapshot_path.isnot(None),
                CameraEvent.trigger_time >= window_start,
                CameraEvent.trigger_time <= window_end,
            )
            .order_by(
                text(f"ABS(EXTRACT(EPOCH FROM (trigger_time - '{vm.trigger_time}'::timestamp)))")
            )
            .first()
        )

        if nearest:
            vm.snapshot_path = nearest.snapshot_path
            print(f"  [PATCH] vehicleMatchResult id={vm.id} -> CDN from id={nearest.id}: {nearest.snapshot_path[:80]}...")
        else:
            print(f"  [WARN]  No matching AccessControllerEvent found for id={vm.id} at {vm.trigger_time}")

    db.flush()

    # ── Step 2: Check entry_exit_log for third vehicle ────────────────────────
    entries = (
        db.query(EntryExitLog)
        .filter(EntryExitLog.gate == "entry")
        .order_by(EntryExitLog.event_time)
        .all()
    )
    print(f"\nCurrent entry_exit_log entries:")
    for e in entries:
        print(f"  id={e.id}  plate={e.plate_number}  time={e.event_time}  snap={e.snapshot_path or '(none)'}")

    # Find the third ANPR camera_event (06:19:07) to get its CDN URL
    third_event = (
        db.query(CameraEvent)
        .filter(
            CameraEvent.camera_id  == "CAM-ENTRY",
            CameraEvent.event_type == "AccessControllerEvent",
            CameraEvent.trigger_time >= datetime(2026, 3, 11, 6, 18, 0),
            CameraEvent.trigger_time <= datetime(2026, 3, 11, 6, 21, 0),
        )
        .first()
    )

    if third_event:
        print(f"\nThird ANPR event: id={third_event.id} time={third_event.trigger_time}")
        print(f"  snapshot: {third_event.snapshot_path}")

        # Check if there's already an entry for this time window
        existing_third = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.gate == "entry",
                EntryExitLog.event_time >= datetime(2026, 3, 11, 6, 18, 0),
                EntryExitLog.event_time <= datetime(2026, 3, 11, 6, 21, 0),
            )
            .first()
        )

        if existing_third:
            print(f"  [SKIP] entry_exit_log already exists: id={existing_third.id} plate={existing_third.plate_number}")
        else:
            # Create entry for third vehicle — plate unknown from camera push
            # Using "PLATE-UNKNOWN" as placeholder; user can correct via DB
            new_entry = EntryExitLog(
                plate_number      = "UNKNOWN-3",
                camera_id         = "CAM-ENTRY",
                gate              = "entry",
                event_time        = third_event.trigger_time,
                snapshot_path     = third_event.snapshot_path,
                is_test           = False,
                created_at        = datetime.utcnow(),
            )
            db.add(new_entry)
            db.flush()
            print(f"  [INSERT] entry_exit_log id={new_entry.id} plate=UNKNOWN-3  (update plate when known)")
    else:
        print("\n  [WARN] Third ANPR event not found in time window 06:18-06:21")

    db.commit()
    print("\nDone.")

except Exception as e:
    db.rollback()
    print(f"ERROR: {e}")
    raise
finally:
    db.close()
