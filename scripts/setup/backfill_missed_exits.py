"""Close sessions stranded by an ingest outage, using HikCentral as the witness.

WHY THIS EXISTS
---------------
On 2026-08-09 PMS-AI stopped receiving camera events for roughly four hours.
HikCentral kept recording normally — 32 entries and 30 exits that day — but 25
of those exits never reached us, so their sessions stayed `status='open'` and
the dashboard reported healthy commuters as 24h+ overstays. None of them
overstayed.

The live reconciler (`_reconcile_missed_exits`) is built for exactly this and
would have healed it, except `HIK_RECONCILE_LOOKBACK_SECONDS` is 900 — fifteen
minutes. When the pod came back at 18:21 it could only see back to 18:06;
everything from 14:18 onward was already out of reach. This script is that same
sweep with an explicit window instead of the rolling one.

THE RE-ENTRY TRAP
-----------------
`parking_session_service.open_session` REUSES an existing open session rather
than creating a second one. So a car that exited during the blackout and drove
back in the next morning did NOT get a fresh session — its live entry was
absorbed into the stale row, which still carries the OLD entry_time. Closing
that row at the blackout exit time would therefore strand a car that is
physically in the garage right now with no session at all, and the entry sweep
could not re-open it because its GUID was consumed by validation on the way in.

So this runs in two phases:

  1. CLOSE  — every unconsumed HikCentral exit in the window, at its real
              pass_time, through the normal reconcile path (audit log, image,
              GUID consumption, duration all handled).
  2. REOPEN — for each plate just closed, if HikCentral shows a LATER entry and
              the plate now has no open session, open one at that entry time.
              The gate log row for it already exists from the live capture; only
              the ParkingSession was swallowed.

Phase 2 is what makes this safe to run while the garage is occupied. Skip it
with --no-reopen only if you are running against a window that ends after the
garage emptied.

USAGE
-----
    python scripts/setup/backfill_missed_exits.py --from "2026-08-09 12:00" \
                                                  --to   "2026-08-09 19:00"
    python scripts/setup/backfill_missed_exits.py --from ... --to ... --apply

Times are facility-local naive (UTC+3 here) — the same clock the HikCentral
export prints, so you can paste straight from the xlsx.

RUN IT INSIDE THE POD
---------------------
The repo `.env` carries HIK_VALIDATION_MODE=off. With the layer off,
`list_unconsumed_records` returns [] and this script cheerfully reports
"nothing to close" — which looks identical to success. Production runs
authoritative. Use `kubectl exec` so env, DB and reach to the platform are all
the deployed ones. The banner below prints the mode it actually resolved; if it
does not say `authoritative`, stop.

SAFETY
------
Reports only by default. Overlapping windows are idempotent — a pass whose GUID
is already in `hik_validations` is skipped — so re-running is harmless.
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.database import SessionLocal  # noqa: E402
from app.services import hikcentral, parking_session_service  # noqa: E402
from app.services.hikcentral import client  # noqa: E402
from app.services.hikcentral.models import from_facility_naive  # noqa: E402
from app.services.entry_exit_service import _reconcile_missed_exits  # noqa: E402
from app.config import settings  # noqa: E402


def _parse_when(raw: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"unrecognised time {raw!r} — use 'YYYY-MM-DD HH:MM' (facility-local)"
    )


async def _run(args) -> int:
    begin, end = args.begin, args.end
    mode = "authoritative" if hikcentral.is_authoritative() else (
        "shadow" if hikcentral.is_enabled() else "OFF"
    )
    print(f"HikCentral: {settings.HIK_BASE_URL}  mode={mode}")
    print(f"Window    : {begin} -> {end}  (facility-local)")
    print(f"Exit cam  : resource_ids={settings.hik_exit_resource_ids() or '(unset)'}")
    if mode != "authoritative":
        print("\nABORT: the HikCentral layer is not authoritative here, so the sweep "
              "cannot close anything. Run this inside the production pod.")
        return 2
    if not settings.hik_exit_resource_ids():
        print("\nABORT: HIK_EXIT_RESOURCE_IDS is unset — nothing to query.")
        return 2

    db = SessionLocal()
    try:
        records = await hikcentral.list_unconsumed_records(
            settings.hik_exit_resource_ids(), begin, end, db
        )
        if not records:
            print("\nNo unconsumed exit records in that window — nothing to do.")
            return 0
        if len(records) >= settings.HIK_RECONCILE_PAGE_SIZE:
            # query_vehicle_logs takes page 1 only, newest-first, so a full page
            # means the OLDEST records in this window were dropped — the exact
            # ones a backfill is looking for. Fail loudly rather than quietly
            # doing a partial job.
            print(f"\nABORT: got {len(records)} records, the page limit "
                  f"({settings.HIK_RECONCILE_PAGE_SIZE}). The query does not "
                  f"paginate, so older records in this window were silently "
                  f"dropped. Re-run over shorter sub-windows.")
            return 2

        print(f"\n{len(records)} unconsumed HikCentral exit(s):\n")
        print(f"  {'PLATE':<12} {'HIK EXIT':<21} {'OPEN SESSION (entry_time)':<28} ACTION")
        print("  " + "-" * 84)
        plan = []
        for rec in sorted(records, key=lambda r: hikcentral.polled_outcome(r).pass_time_local):
            outcome = hikcentral.polled_outcome(rec)
            when = outcome.pass_time_local
            sess = parking_session_service.get_latest_open_session(db, rec.canonical_plate)
            if sess is not None and sess.entry_time <= when:
                action, target = "CLOSE", str(sess.entry_time)
            elif sess is not None:
                action, target = "skip (session newer than exit)", str(sess.entry_time)
            else:
                action, target = "log only (no open session)", "-"
            print(f"  {rec.canonical_plate:<12} {str(when):<21} {target:<28} {action}")
            plan.append((rec.canonical_plate, when))

        if not args.apply:
            print("\nDRY RUN — nothing changed. Verify these against the HikCentral "
                  "export, then re-run with --apply.")
            return 0

        await _reconcile_missed_exits(db, window=(begin, end))
        db.commit()
        print(f"\nPhase 1 done — swept {len(records)} exit record(s).")

        if args.no_reopen:
            print("Phase 2 skipped (--no-reopen).")
            return 0

        reopened = 0
        for plate, closed_at in plan:
            if parking_session_service.get_latest_open_session(db, plate) is not None:
                continue  # still has a session; nothing was stranded
            # NOT list_unconsumed_records: these return entries were captured
            # live, so validation already consumed their GUIDs and that helper
            # would filter out exactly the cars phase 2 exists for. Reopening
            # via open_session needs no unconsumed GUID — the gate log row
            # already exists; only the ParkingSession was swallowed by the
            # merge into the stale session we just closed.
            later = await client.query_vehicle_logs(
                from_facility_naive(closed_at),
                from_facility_naive(args.reopen_until),
                settings.hik_entry_resource_ids(),
                settings.HIK_RECONCILE_PAGE_SIZE,
            )
            entries = sorted(
                hikcentral.polled_outcome(r).pass_time_local
                for r in later if r.canonical_plate == plate
            )
            if not entries:
                continue
            parking_session_service.open_session(
                db, plate_number=plate, event_time=entries[-1],
                camera_id="CAM-ENTRY", snapshot_path=None,
            )
            db.commit()
            reopened += 1
            print(f"  REOPENED {plate} at {entries[-1]} (drove back in after the outage)")

        print(f"\nPhase 2 done — reopened {reopened} session(s) for cars that returned.")
        print("Zone counts recompute from open sessions on the next gate event.")
        return 0
    finally:
        db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="begin", required=True, type=_parse_when,
                    metavar="'YYYY-MM-DD HH:MM'", help="window start, facility-local")
    ap.add_argument("--to", dest="end", required=True, type=_parse_when,
                    metavar="'YYYY-MM-DD HH:MM'", help="window end, facility-local")
    ap.add_argument("--reopen-until", type=_parse_when, default=None,
                    metavar="'YYYY-MM-DD HH:MM'",
                    help="how far forward phase 2 looks for a return entry "
                         "(default: now)")
    ap.add_argument("--no-reopen", action="store_true",
                    help="skip phase 2 — only safe if the garage emptied after "
                         "the window")
    ap.add_argument("--apply", action="store_true",
                    help="actually mutate (default: report only)")
    args = ap.parse_args()
    if args.end <= args.begin:
        ap.error("--to must be after --from")
    if args.reopen_until is None:
        from app.config import facility_now_naive
        args.reopen_until = facility_now_naive()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
