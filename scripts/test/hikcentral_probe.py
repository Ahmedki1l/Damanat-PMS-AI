#!/usr/bin/env python
"""Probe the REAL HikCentral platform — no mocks, no fixtures.

Answers the questions unit tests structurally cannot:
  * do these credentials authenticate over Digest?
  * what does the VehicleLogs envelope actually look like on THIS build?
  * is the configured resource ID really the entry (or exit) LPR camera?
  * do the `Vsm://` handles download, and are they really images?
  * does a real PlateLicense normalize to the letters-first form the DB stores?

Usage (nothing is written to the database):

    python scripts/test/hikcentral_probe.py \
        --base-url https://hikcentral.example:443 \
        --user admin --password 'secret' \
        --resource-ids 447 --minutes 30

Credentials may also come from .env (HIK_BASE_URL / HIK_USERNAME /
HIK_PASSWORD / HIK_ENTRY_RESOURCE_IDS); CLI flags win. Add --download to pull
the first record's imagery to disk, and --raw to dump the untouched JSON
envelope, which is the fastest way to see if the parser needs adjusting.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

# Import the app package when run from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import httpx  # noqa: E402


VEHICLE_LOGS_PATH = "/ISAPI/Bumblebee/VehicleBiz/V0/LPR/VehicleLogs"
PICTURE_PATH = "/ISAPI/Bumblebee/Platform/V0/Storage/Picture"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def _parse_args() -> argparse.Namespace:
    from app.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=settings.HIK_BASE_URL)
    parser.add_argument("--user", default=settings.HIK_USERNAME)
    parser.add_argument("--password", default=settings.HIK_PASSWORD)
    parser.add_argument(
        "--resource-ids",
        default=settings.HIK_ENTRY_RESOURCE_IDS or "",
        help="Comma-separated HikCentral resource IDs, e.g. 447",
    )
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--page-size", type=int, default=5)
    parser.add_argument("--insecure", action="store_true", help="Skip TLS verify")
    parser.add_argument("--raw", action="store_true", help="Dump the JSON envelope")
    parser.add_argument(
        "--download", action="store_true", help="Fetch the first record's images"
    )
    return parser.parse_args()


async def main() -> int:
    args = _parse_args()
    if not args.base_url or not args.user or not args.password:
        print(
            "ERROR: --base-url, --user and --password are required "
            "(or set HIK_BASE_URL / HIK_USERNAME / HIK_PASSWORD in .env)"
        )
        return 2
    if not args.resource_ids:
        print("ERROR: --resource-ids is required (e.g. 447)")
        return 2

    from app.config import facility_tz
    from app.services.event_parser import normalize_plate
    from app.services.hikcentral.client import _extract_raw_records
    from app.services.hikcentral.models import VehicleLogRecord, to_facility_naive

    now = datetime.now(facility_tz())
    begin = now - timedelta(minutes=args.minutes)
    body = {
        "VehicleLogsRequest": {
            "PageIndex": 1,
            "PageSize": args.page_size,
            "SearchCriteria": {
                "ResourceType": 0,
                "RequestTimeType": 0,
                "BeginTime": begin.strftime(TIME_FORMAT),
                "EndTime": now.strftime(TIME_FORMAT),
                "ResourceIDs": args.resource_ids,
            },
            "RequestSortType": {"SortType": 1},
        }
    }

    print(f"POST {args.base_url}{VEHICLE_LOGS_PATH}")
    print(f"  window     : {begin.isoformat()} .. {now.isoformat()}")
    print(f"  resourceIDs: {args.resource_ids}\n")

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        auth=httpx.DigestAuth(args.user, args.password),
        timeout=httpx.Timeout(connect=5.0, read=15.0, write=15.0, pool=5.0),
        verify=not args.insecure,
    ) as client:
        try:
            response = await client.post(VEHICLE_LOGS_PATH, json=body)
        except Exception as exc:  # noqa: BLE001 - a probe reports, never crashes
            print(f"TRANSPORT FAILURE: {type(exc).__name__}: {exc}")
            return 1

        print(f"HTTP {response.status_code}")
        if response.status_code == 401:
            print("  -> Digest auth rejected. Wrong credentials, or the account "
                  "lacks API privileges.")
            return 1
        if response.status_code >= 400:
            print(f"  body: {response.text[:500]}")
            return 1

        try:
            payload = response.json()
        except ValueError:
            print(f"  NON-JSON body: {response.text[:500]}")
            return 1

        if args.raw:
            print("\n--- raw envelope ---")
            print(json.dumps(payload, indent=2, ensure_ascii=False)[:4000])
            print("--- end raw ---\n")

        raw_records = _extract_raw_records(payload)
        print(f"\nparser found {len(raw_records)} record(s) in the envelope")
        if not raw_records:
            print(
                "  -> No records. Either no vehicle passed in this window, the "
                "resource ID is wrong, or the envelope shape is unexpected "
                "(re-run with --raw to see it)."
            )
            return 1

        print(f"  top-level keys: {list(payload)[:6]}\n")

        first = None
        for index, raw in enumerate(raw_records, start=1):
            record = VehicleLogRecord.from_payload(raw)
            if record is None:
                print(f"[{index}] UNPARSABLE: {json.dumps(raw)[:200]}")
                continue
            first = first or record
            print(f"[{index}] GUID          : {record.guid}")
            print(f"    PassTime      : {record.pass_time.isoformat()}")
            print(f"    -> DB-local   : {to_facility_naive(record.pass_time)}")
            print(f"    PlateLicense  : {record.plate_license!r}")
            print(f"    -> normalized : {record.canonical_plate!r}"
                  f"   (DB stores letters-first)")
            print(f"    Resource      : {record.resource_id} / {record.resource_name}")
            print(f"    VehicleType   : {record.vehicle_type}")
            print(f"    Direction     : {record.vehicle_direction_type}")
            print(f"    VehicleImage  : {(record.vehicle_image_url or '')[:70]}")
            print(f"    PlateImage    : {(record.plate_image_url or '')[:70]}")
            if record.plate_license and not record.canonical_plate:
                print("    !! normalize_plate() REJECTED this plate — matching "
                      "would never succeed for this car.")
            print()

        if args.download and first is not None:
            for kind, url in (
                ("vehicle", first.vehicle_image_url),
                ("plate", first.plate_image_url),
            ):
                if not url:
                    continue
                print(f"GET {PICTURE_PATH}?URL={url[:50]}...")
                try:
                    picture = await client.get(PICTURE_PATH, params={"URL": url})
                except Exception as exc:  # noqa: BLE001
                    print(f"  TRANSPORT FAILURE: {type(exc).__name__}: {exc}")
                    continue
                content_type = picture.headers.get("content-type", "?")
                print(f"  HTTP {picture.status_code} "
                      f"content-type={content_type} bytes={len(picture.content)}")
                if picture.status_code == 200 and picture.content:
                    name = f"hik_probe_{kind}_{first.guid[:12]}.jpg"
                    with open(name, "wb") as handle:
                        handle.write(picture.content)
                    print(f"  saved -> {name}")
                    if not content_type.lower().startswith("image/"):
                        print("  !! not an image content-type; the client would "
                              "reject this download.")

    print("\nOK — HikCentral answered and the parser understood the response.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
