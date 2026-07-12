"""Recover entry/exit events the backend resolved but never committed.

Why this exists
---------------
On 2026-07-12 the entry flusher wrote entry_exit_log + parking_sessions and then
awaited a PMS forward BEFORE committing. A concurrent handler blocked on those
uncommitted rows and — because pyodbc is synchronous on the asyncio event loop —
froze the whole service. When the process was killed the transaction rolled back,
so an entry the log clearly reports as CONFIRMED never reached the table.

That ordering bug is fixed (see entry_exit_service._flush_entry_burst). This
script repairs the DATA the old bug destroyed, on any deployment.

What it can and cannot recover
------------------------------
CAN:    events the backend actually resolved — i.e. present in logs/events.log as
        "[UC1] Entry CONFIRMED (burst flush): plate=X" or "[UC1] Gate=exit |
        Plate=X" — but with no matching row in entry_exit_log. Typically the one
        in-flight entry that was rolled back when the process died.

CANNOT: cars that passed while the service was frozen. The backend accepted
        nothing then, so they appear in NO log, NO snapshot and NO spool. Those
        exist only in the cameras' own ANPR records and are out of scope here.

The plate, gate, camera and snapshot are recovered exactly. The event_time is
reconstructed from the ANPR snapshot filename
(detection_images/snapshot_ANPR_<CAM>_<YYYYMMDD_HHMMSS_ffffff>.jpg), because the
camera's own trigger time is never written to the log.

ACCURACY: that makes event_time up to ~2s EARLY. Measured against 35 known-good
rows on 2026-07-12, event_time - snapshot_time was 0.00s min / 1.27s median /
1.81s max. So a recovered row's timestamp is right to within about two seconds,
not to the millisecond — which shifts a computed parking duration by ~1s. Fine
for occupancy and billing at minute granularity; do not treat it as exact.

Usage (run from the backend root, against whatever DB .env points at):
    python scripts/recover_missing_entries.py            # dry run, prints a plan
    python scripts/recover_missing_entries.py --apply    # writes

Idempotent: an event already present in entry_exit_log is skipped, so re-running
is safe.
"""

import argparse
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal                      # noqa: E402
from app.models.entry_exit_log import EntryExitLog         # noqa: E402
from app.services import parking_session_service           # noqa: E402
from app.services import vehicle_service                   # noqa: E402
from app.config import settings, facility_now_naive        # noqa: E402

LOG_TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
RE_ENTRY = re.compile(LOG_TS + r".*\[UC1\] Entry CONFIRMED \(burst flush\): plate=(\S+)")
RE_EXIT = re.compile(LOG_TS + r".*\[UC1\] Gate=exit \| Plate=(\S+)")
RE_SNAP = re.compile(
    r"snapshot_ANPR_(?P<cam>[\w-]+)_(?P<ts>\d{8}_\d{6}_\d{6})\.jpg"
)
# Plates used by the test-suite; they leak into events.log because tests share the
# app logger. Never recover these into a real table.
FAKE_PLATES = {"ABC-1234", "TEST-123", "NO-ENTRY", "RIGHT-1234", "OKAY-0001",
               "WRONG-9999", "XYZ-0000"}
# A log line is emitted a few seconds after the camera's trigger time.
MATCH_WINDOW = timedelta(seconds=120)


def parse_log(path: str, since: datetime) -> list[dict]:
    """Every entry/exit the backend RESOLVED, from the app log."""
    found = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            for pattern, gate in ((RE_ENTRY, "entry"), (RE_EXIT, "exit")):
                m = pattern.match(line)
                if not m:
                    continue
                plate = m.group(2)
                logged_at = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if plate in FAKE_PLATES or logged_at < since:
                    continue
                found.append({"plate": plate, "gate": gate, "logged_at": logged_at})
    return found


def index_snapshots(directory: str) -> list[tuple[datetime, str, str]]:
    """(trigger_time, camera_id, filename) for every ANPR snapshot on disk."""
    out = []
    if not os.path.isdir(directory):
        return out
    for name in os.listdir(directory):
        m = RE_SNAP.search(name)
        if not m:
            continue
        try:
            ts = datetime.strptime(m.group("ts"), "%Y%m%d_%H%M%S_%f")
        except ValueError:
            continue
        out.append((ts, m.group("cam"), name))
    return sorted(out)


def snapshot_for(event: dict, snaps: list) -> tuple[datetime, str, str] | None:
    """The ANPR snapshot that triggered this event: the newest one from a camera
    matching the gate, at or before the log line, within the match window."""
    want = "CAM-ENTRY" if event["gate"] == "entry" else "CAM-EXIT"
    best = None
    for ts, cam, name in snaps:
        if cam != want or ts > event["logged_at"]:
            continue
        if event["logged_at"] - ts > MATCH_WINDOW:
            continue
        best = (ts, cam, name)   # sorted ascending → last match is the closest
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to the DB")
    ap.add_argument("--log", default="logs/events.log")
    ap.add_argument("--images", default="detection_images")
    ap.add_argument("--since", default=datetime.now().strftime("%Y-%m-%d"),
                    help="only consider events on/after this date (YYYY-MM-DD)")
    args = ap.parse_args()

    since = datetime.strptime(args.since, "%Y-%m-%d")
    if not os.path.exists(args.log):
        print(f"log not found: {args.log}")
        return 1

    resolved = parse_log(args.log, since)
    snaps = index_snapshots(args.images)
    db = SessionLocal()

    print(f"DB       : {db.bind.url.render_as_string(hide_password=True)}")
    print(f"log      : {args.log}")
    print(f"since    : {since:%Y-%m-%d}")
    print(f"resolved in log: {len(resolved)}\n")

    missing, unrecoverable = [], []
    for ev in resolved:
        lo = ev["logged_at"] - MATCH_WINDOW
        hi = ev["logged_at"] + MATCH_WINDOW
        already = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == ev["plate"],
                EntryExitLog.gate == ev["gate"],
                EntryExitLog.event_time >= lo,
                EntryExitLog.event_time <= hi,
            )
            .first()
        )
        if already:
            continue
        snap = snapshot_for(ev, snaps)
        if snap is None:
            unrecoverable.append(ev)
            continue
        ev["event_time"], _, ev["snapshot_file"] = snap
        ev["camera_id"] = "CAM-ENTRY" if ev["gate"] == "entry" else "CAM-EXIT"
        missing.append(ev)

    if unrecoverable:
        print("Resolved in the log but NO snapshot on disk — cannot rebuild "
              "event_time/snapshot, skipping:")
        for ev in unrecoverable:
            print(f"  {ev['logged_at']}  {ev['plate']}  {ev['gate']}")
        print()

    if not missing:
        print("Nothing to recover: every resolved event is already in "
              "entry_exit_log.")
        return 0

    print(f"MISSING from entry_exit_log ({len(missing)}):")
    for ev in missing:
        print(f"  {ev['plate']:<12} {ev['gate']:<5} event_time={ev['event_time']} "
              f"snapshot={ev['snapshot_file']}")

    if not args.apply:
        db.rollback()
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return 0

    base = settings.PUBLIC_BASE_URL.rstrip("/")
    for ev in missing:
        vehicle = vehicle_service.ensure_unregistered_vehicle(db, ev["plate"])
        snapshot_path = f"{base}/snapshots/{ev['snapshot_file']}"
        db.add(EntryExitLog(
            plate_number=ev["plate"],
            vehicle_id=vehicle.id if vehicle else None,
            vehicle_type=vehicle.vehicle_type if vehicle else "unknown",
            gate=ev["gate"],
            camera_id=ev["camera_id"],
            event_time=ev["event_time"],
            snapshot_path=snapshot_path,
            created_at=facility_now_naive(),
        ))
        if ev["gate"] == "entry":
            parking_session_service.open_session(
                db,
                plate_number=ev["plate"],
                event_time=ev["event_time"],
                camera_id=ev["camera_id"],
                snapshot_path=snapshot_path,
                vehicle=vehicle,
            )
        else:
            parking_session_service.close_session(
                db,
                plate_number=ev["plate"],
                event_time=ev["event_time"],
                camera_id=ev["camera_id"],
                snapshot_path=snapshot_path,
            )
        db.commit()
        print(f"  recovered {ev['plate']} ({ev['gate']})")

    print(f"\nAPPLIED: {len(missing)} event(s) recovered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
