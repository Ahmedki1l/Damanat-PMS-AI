"""Probe: what does crossRecords ACTUALLY hold in one window, unfiltered?

WHY THIS EXISTS. Every production path that reads crossRecords drops a record
with no readable plate before anything can see it:

    validation.py:592   list_unconsumed_records -> if r.canonical_plate and ...
    validation.py       filter_recoverable      -> same test

Nothing logs the drop. So "0 candidate(s) in window" and "the reconciler found
nothing" are both consistent with a record that EXISTS and whose plateLicense
came back empty. On 2026-09-09 a car crossed the CAM-23 ramp line at 09:02:56
with no ANPR read at all; the question this settles is whether HikCentral is
equally blind to it, or whether it holds a plateless pass nobody could see.

This dumps the raw rows. No plate filter, no GUID dedup, no parsing into
VehicleLogRecord. Read-only: it queries, prints, and writes nothing.

READ THE ANSWER BY CONTENT, NEVER BY ABSENCE OF ERROR. An unknown
cameraIndexCode answers HTTP 200 / code=0 / empty list, byte-for-byte
identical to a camera that genuinely had no passes -- the trap that let
HIK_EXIT_RESOURCE_IDS=453 point at a nonexistent camera for months. So this
script prints the code it got and the resource it asked about, every time.

Usage:
    python scripts/setup/probe_hik_crossrecords_raw.py \
        --from 2026-09-09T09:01:30 --to 2026-09-09T09:04:30
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.services.event_parser import normalize_plate  # noqa: E402
from app.services.hikcentral import client  # noqa: E402


def _tz() -> timezone:
    return timezone(
        timedelta(hours=float(settings.FACILITY_TIMEZONE_OFFSET_HOURS or 0.0))
    )


def _aware(local_naive: str) -> datetime:
    """'2026-09-09T09:01:30' -> aware datetime in facility-local time."""
    return datetime.fromisoformat(local_naive).replace(tzinfo=_tz())


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="begin", required=True,
                        help="facility local time, e.g. 2026-09-09T09:01:30")
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--resource", default="",
                        help="camera indexCode; defaults to the configured entry camera")
    parser.add_argument("--page-size", type=int, default=100)
    args = parser.parse_args()

    resource = (args.resource or settings.hik_entry_resource_ids() or "").split(",")[0].strip()
    if not resource:
        print("No camera indexCode. Pass --resource or set HIK_ENTRY_RESOURCE_IDS.")
        return

    begin, end = _aware(args.begin), _aware(args.end)
    body = {
        "cameraIndexCode": resource,
        "startTime": begin.replace(microsecond=0).isoformat(),
        "endTime": end.replace(microsecond=0).isoformat(),
        "pageNo": 1,
        "pageSize": args.page_size,
        "sortField": "PassTime",
        "orderType": 1,
    }

    print(f"HikCentral base_url={settings.HIK_BASE_URL}")
    print(f"Asking {client.CROSS_RECORDS_PATH}")
    print(f"  cameraIndexCode={resource}  {body['startTime']} .. {body['endTime']}\n")

    response = await client._signed_post(client.CROSS_RECORDS_PATH, body)
    if response is None:
        print("  UNREACHABLE - no usable response (transport or auth).")
        return
    try:
        payload = response.json()
    except ValueError:
        print(f"  non-JSON response (HTTP {response.status_code}): {response.text[:200]}")
        return

    code = client.response_code(payload)
    if code != "0":
        print(f"  REFUSED - code={code}. An empty result below would mean NOTHING.")
        print(f"  raw: {json.dumps(payload)[:400]}")
        return

    data = payload.get("data") or {}
    rows = data.get("list") or []
    print(f"  AUTHORIZED - total={data.get('total')} returned={len(rows)}\n")
    if not rows:
        print("  The call succeeded and the window held NO crossRecords at all.")
        print(f"  Confirm the indexCode is real before trusting this: an unknown")
        print(f"  code returns exactly this. Cross-check {resource} against")
        print("  probe_hik_camera_events.py --list-cameras.")
        return

    invisible = 0
    for row in rows:
        # crossRecords/page is parsed by VehicleLogRecord.from_openapi_record,
        # so THESE are the keys that matter -- not the web VehicleLogs spelling
        # (GUID / PassTime / PlateLicense), which this endpoint does not use.
        plate = str(row.get("plateNo") or "").strip()
        canonical = normalize_plate(plate)
        # The exact test that hides a record from production:
        #   validation.py:592  if r.canonical_plate and not guid_already_used(..)
        # It rejects "", "N/A", "NONE", "NULL", "UNKNOWN" and OCR garbage that is
        # all-digits or all-letters -- a wider hole than "empty string".
        hidden = canonical is None
        invisible += hidden
        print(f"    {str(row.get('crossTime') or '-'):<28} "
              f"plateNo={(plate or '(EMPTY)'):<14} "
              f"canonical={str(canonical):<12} "
              f"{'<-- INVISIBLE to production' if hidden else ''}")
        print(f"      crossRecordSyscode={row.get('crossRecordSyscode')}  "
              f"pic={'yes' if row.get('vehiclePicUri') else 'no'}")

    print(f"\n  {len(rows)} record(s); {invisible} that normalize_plate() rejects.")
    if invisible:
        print("  Those are INVISIBLE to list_unconsumed_records and")
        print("  filter_recoverable (both test `if r.canonical_plate`), which is")
        print("  why the reconciler reported nothing to open. THE PASS EXISTS.")
    else:
        print("  Every record here carries a usable plate, so nothing was hidden")
        print("  by the plate filter in this window.")
    print("\n--- raw ---")
    print(json.dumps(rows, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
