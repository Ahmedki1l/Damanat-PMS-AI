"""Probe: does HikCentral actually hold VCA events for the ramp cameras?

THIS IS THE GATE FOR THE DROPPED-ANPR RECOVERY SWEEP. If HikCentral is not
receiving line-crossing events from CAM-23 and CAM-03, then no amount of code
here can recover a dropped gate read from them, and the work is a platform
configuration task rather than an engineering one. Run this before enabling
HIK_RAMP_RESOURCE_IDS, and read the answer carefully.

READ THE ANSWER BY CONTENT, NEVER BY ABSENCE OF ERROR.

    An unknown indexCode returns HTTP 200, code=0, and an empty list.

That is byte-for-byte what a camera with genuinely no events in the window
returns. It is how HIK_EXIT_RESOURCE_IDS=453 came to point at a camera that
does not exist while the exit reconciler swept happily for months and never
closed a single exit. So this script separates the cases it can:

    NO SUCH CAMERA      the indexCode is not in the platform's camera list
    NO EVENTS           the camera exists, but the window held nothing
    EVENTS FLOWING      the camera exists and events came back

Read-only. It queries, prints, and writes nothing.

Usage:
    # 1. What cameras does the platform know about, and what are their codes?
    python scripts/setup/probe_hik_camera_events.py --list-cameras

    # 2. Do those codes actually produce VCA events?
    python scripts/setup/probe_hik_camera_events.py --codes <a>,<b> --hours 24

NOTE ON THE CODES YOU ALREADY HAVE. 447 is the ANPR-1 ENTRY camera and 510 is
the ANPR-2 EXIT camera. Neither is a ramp camera. CAM-23 and CAM-03 are
different devices whose indexCodes have never been recorded anywhere in this
repository - finding them is what step 1 is for. Do not carry an LPR code over
into HIK_RAMP_RESOURCE_IDS because it is the one you happen to know.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import facility_now_naive, settings  # noqa: E402
from app.services import hikcentral  # noqa: E402
from app.services.hikcentral import client  # noqa: E402
from app.services.hikcentral.models import from_facility_naive  # noqa: E402

CAMERA_PATHS = (
    "/artemis/api/resource/v1/cameras",
    "/artemis/api/resource/v2/camera/search",
)


async def list_cameras() -> dict[str, str]:
    """Every camera the platform will admit to, as {indexCode: name}."""
    found: dict[str, str] = {}
    for path in CAMERA_PATHS:
        response = await client._signed_post(
            path, {"pageNo": 1, "pageSize": 200}
        )
        if response is None:
            print(f"  {path}: no response")
            continue
        try:
            payload = response.json()
        except ValueError:
            print(f"  {path}: non-JSON response")
            continue
        code = client.response_code(payload)
        if code != "0":
            print(f"  {path}: refused with code={code}")
            continue
        rows = ((payload.get("data") or {}).get("list")) or []
        for row in rows:
            index_code = str(row.get("cameraIndexCode") or "")
            if index_code:
                found[index_code] = str(row.get("cameraName") or "")
        print(f"  {path}: {len(rows)} camera(s)")
    return found


async def probe_events(codes: str, hours: float) -> None:
    end = facility_now_naive()
    begin = end - timedelta(hours=hours)
    print(f"\nWindow: {begin} .. {end}  ({hours}h)")

    known = await list_cameras()
    requested = [item.strip() for item in codes.split(",") if item.strip()]

    print("\n--- indexCode sanity ---")
    unknown = []
    for code in requested:
        if code in known:
            print(f"  {code}: EXISTS  ({known[code]})")
        else:
            unknown.append(code)
            print(f"  {code}: *** NOT IN THE PLATFORM'S CAMERA LIST ***")
    if unknown:
        print(
            "\n  An unknown indexCode still answers 200/code=0/empty. Any sweep\n"
            "  configured with one would look healthy and recover nothing.\n"
            "  Fix these before setting HIK_RAMP_RESOURCE_IDS."
        )

    print("\n--- eventRecords ---")
    records = await client.list_camera_events(
        begin=from_facility_naive(begin),
        end=from_facility_naive(end),
        resource_ids=codes,
        page_size=200,
    )
    print(f"  returned {len(records)} event(s)")

    if not records:
        verdict = (
            "NO SUCH CAMERA (see above) - fix the codes and re-run"
            if unknown
            else "NO EVENTS - the cameras exist but the platform holds no VCA\n"
            "           events for them in this window. Widen --hours; if it\n"
            "           stays empty, HCP is not receiving line-crossing events\n"
            "           from these cameras and dropped-ANPR recovery is a\n"
            "           PLATFORM CONFIGURATION task, not a code task."
        )
        print(f"\nVERDICT: {verdict}")
        return

    with_images = [r for r in records if r.event_pic_uri]
    by_camera: dict[str, int] = {}
    for record in records:
        by_camera[record.src_index] = by_camera.get(record.src_index, 0) + 1

    print(f"  with an image: {len(with_images)}/{len(records)}")
    for src, count in sorted(by_camera.items()):
        print(f"    srcIndex={src}: {count}")
    print("\n  sample:")
    for record in records[:5]:
        print(
            f"    {record.event_index_code}  src={record.src_index}  "
            f"type={record.event_type}  at={record.start_time}  "
            f"image={'yes' if record.event_pic_uri else 'NO'}"
        )

    print(
        "\nVERDICT: EVENTS FLOWING. Recovery is viable for the cameras above.\n"
        f"         Images are what Re-ID needs; {len(with_images)} of "
        f"{len(records)} carried one."
    )
    if not with_images:
        print(
            "         WARNING: no event carried an image. Without one there is\n"
            "         nothing for Re-ID to associate, so an event can never\n"
            "         become a witness - only a hint that something passed."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-cameras", action="store_true")
    parser.add_argument(
        "--codes",
        default=settings.HIK_RAMP_RESOURCE_IDS,
        help="comma-separated camera indexCodes (default: HIK_RAMP_RESOURCE_IDS)",
    )
    parser.add_argument("--hours", type=float, default=24.0)
    args = parser.parse_args()

    if not hikcentral.is_enabled():
        print(
            "HikCentral is not enabled in this environment "
            f"(HIK_VALIDATION_MODE={settings.HIK_VALIDATION_MODE!r}). "
            "Run this inside the pod."
        )
        return 2

    async def run() -> int:
        await client.start_hikcentral_http_client()
        try:
            if args.list_cameras:
                print("--- cameras ---")
                for index_code, name in sorted((await list_cameras()).items()):
                    print(f"  {index_code:>8}  {name}")
                return 0
            if not args.codes:
                print(
                    "No codes given and HIK_RAMP_RESOURCE_IDS is empty.\n"
                    "Run with --list-cameras first."
                )
                return 2
            await probe_events(args.codes, args.hours)
            return 0
        finally:
            await client.close_hikcentral_http_client()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
