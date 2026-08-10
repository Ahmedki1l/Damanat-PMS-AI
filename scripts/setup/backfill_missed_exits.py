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
from app.services.hikcentral import client, validation  # noqa: E402
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


async def _list_cameras() -> int:
    """Read-only. Print every camera this OpenAPI partner can actually see.

    A wrong `cameraIndexCode` and a camera that exists but was never authorised
    to the partner both return an empty list with HTTP 200 and code=0 — there is
    no error to catch. Listing the resources tells them apart: if 'ANPR-2 Exit'
    is absent entirely, the fix is authorising it in HikCentral, not editing
    HIK_EXIT_RESOURCE_IDS.
    """
    for path, body in (
        ("/artemis/api/resource/v1/cameras", {"pageNo": 1, "pageSize": 200}),
        ("/artemis/api/resource/v2/camera/search",
         {"pageNo": 1, "pageSize": 200, "name": ""}),
    ):
        print(f"\n--- {path} ---")
        response = await client._signed_post(path, body)
        if response is None:
            print("  no response (see the warning logged above)")
            continue
        try:
            payload = response.json()
        except ValueError:
            print(f"  non-JSON: {response.text[:200]}")
            continue
        code = client.response_code(payload)
        if code != "0":
            print(f"  refused code={code} msg={payload.get('msg')}")
            continue
        rows = ((payload.get("data") or {}).get("list")) or []
        print(f"  {len(rows)} camera(s) visible to appKey={settings.HIK_APP_KEY}\n")
        print(f"  {'indexCode':<38} name")
        for cam in rows:
            index_code = cam.get("cameraIndexCode") or cam.get("indexCode") or "?"
            name = cam.get("cameraName") or cam.get("name") or "?"
            flag = ""
            if index_code == settings.hik_exit_resource_ids():
                flag = "   <-- currently HIK_EXIT_RESOURCE_IDS"
            elif index_code == settings.hik_entry_resource_ids():
                flag = "   <-- currently HIK_ENTRY_RESOURCE_IDS"
            print(f"  {str(index_code):<38} {name}{flag}")
        if rows:
            print("\n  Find the row named like 'ANPR-2 Exit' and put ITS indexCode "
                  "in HIK_EXIT_RESOURCE_IDS (deployed config, not repo .env).")
            print("  If no exit camera is listed at all, it is not authorised to "
                  "this OpenAPI partner — fix that in HikCentral first.")
            return 0
    return 0


async def _diagnose(db, begin, end) -> int:
    """Read-only. Why did the sweep find nothing?

    "No unconsumed exit records" collapses three very different causes into one
    message, so this pulls the raw platform response apart and shows which one
    it is:

      * the platform returned nothing for this camera/window  -> wrong
        indexCode, or a timezone mismatch on the query bounds
      * records came back but carry no usable plate           -> normalisation
      * records came back and every GUID is already consumed  -> the passes were
        recorded; the sessions were left open for some OTHER reason, and closing
        them is not this script's job
    """
    from app.models.hik_validation import HikValidation

    for label, resource_ids in (
        ("EXIT ", settings.hik_exit_resource_ids()),
        ("ENTRY", settings.hik_entry_resource_ids()),
    ):
        raw = await client.query_vehicle_logs(
            from_facility_naive(begin), from_facility_naive(end),
            resource_ids, settings.HIK_RECONCILE_PAGE_SIZE,
        )
        plateless = [r for r in raw if not r.canonical_plate]
        consumed = [r for r in raw if r.canonical_plate
                    and validation.guid_already_used(db, r.guid)]
        fresh = [r for r in raw if r.canonical_plate
                 and not validation.guid_already_used(db, r.guid)]
        print(f"\n{label} camera (resource_ids={resource_ids or '(unset)'})")
        print(f"  platform returned : {len(raw)}")
        print(f"  no usable plate   : {len(plateless)}")
        print(f"  GUID consumed     : {len(consumed)}")
        print(f"  actionable        : {len(fresh)}")
        for r in sorted(raw, key=lambda r: hikcentral.polled_outcome(r).pass_time_local)[:6]:
            state = ("plateless" if not r.canonical_plate
                     else "consumed" if validation.guid_already_used(db, r.guid)
                     else "ACTIONABLE")
            print(f"    {hikcentral.polled_outcome(r).pass_time_local}  "
                  f"{r.canonical_plate or '-':<12} {state}")

    total = db.query(HikValidation).filter(
        HikValidation.direction == hikcentral.DIRECTION_EXIT,
        HikValidation.pass_time >= begin,
        HikValidation.pass_time <= end,
    ).count()
    print(f"\nhik_validations rows already stored for exits in this window: {total}")
    print("\nReading this:")
    print("  EXIT returned 0 but ENTRY returned rows -> HIK_EXIT_RESOURCE_IDS is "
          "wrong (the export calls the camera 'ANPR-2 Exit'; confirm its OpenAPI "
          "indexCode).")
    print("  BOTH returned 0 -> the query bounds miss the data: check the "
          "timezone the platform stores pass times in.")
    print("  Rows returned but all consumed -> the passes WERE recorded; the "
          "sessions stayed open for another reason. Send me this output.")
    return 0


async def _run(args) -> int:
    if args.list_cameras:
        if not hikcentral.is_enabled():
            print("ABORT: the HikCentral layer is OFF here. Run inside the pod.")
            return 2
        return await _list_cameras()
    begin, end = args.begin, args.end
    mode = "authoritative" if hikcentral.is_authoritative() else (
        "shadow" if hikcentral.is_enabled() else "OFF"
    )
    print(f"HikCentral: {settings.HIK_BASE_URL}  mode={mode}")
    print(f"Window    : {begin} -> {end}  (facility-local)")
    print(f"Exit cam  : resource_ids={settings.hik_exit_resource_ids() or '(unset)'}")
    if mode == "OFF":
        print("\nABORT: the HikCentral layer is OFF here, so no query can be "
              "issued. Run this inside the production pod.")
        return 2
    if mode != "authoritative" and not args.diagnose:
        # --diagnose only reads, so shadow mode is fine for it.
        print("\nABORT: the HikCentral layer is not authoritative here, so the sweep "
              "cannot close anything. Run this inside the production pod.")
        return 2
    if not settings.hik_exit_resource_ids():
        print("\nABORT: HIK_EXIT_RESOURCE_IDS is unset — nothing to query.")
        return 2

    db = SessionLocal()
    try:
        if args.diagnose:
            return await _diagnose(db, begin, end)
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
    ap.add_argument("--from", dest="begin", type=_parse_when,
                    metavar="'YYYY-MM-DD HH:MM'", help="window start, facility-local")
    ap.add_argument("--to", dest="end", type=_parse_when,
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
    ap.add_argument("--list-cameras", dest="list_cameras", action="store_true",
                    help="read-only: print every camera this OpenAPI partner "
                         "can see, with its indexCode. Changes nothing.")
    ap.add_argument("--diagnose", action="store_true",
                    help="read-only: show the raw platform response and why "
                         "records were filtered out. Changes nothing.")
    args = ap.parse_args()
    if not args.list_cameras and (args.begin is None or args.end is None):
        ap.error("--from and --to are required (except with --list-cameras)")
    if not args.list_cameras and args.end <= args.begin:
        ap.error("--to must be after --from")
    if args.reopen_until is None:
        from app.config import facility_now_naive
        args.reopen_until = facility_now_naive()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
