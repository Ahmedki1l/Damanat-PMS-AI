"""Probe: pull real HikCentral vehicle images so the overlay guard can be measured.

WHY. Hikvision composites its own plate/OSD panel into the corner of a frame.
Our plate detector will happily lock onto it — it is sharp, high-contrast and
perfectly rectangular, everything a real plate at twenty metres is not — and
then our "independent" OCR is reading Hikvision's answer back to itself.

ENTRY_V2_OVERLAY_EXCLUDE_REGIONS is what stops that, and it is EMPTY until
somebody measures where the panel actually is on THIS site's frames. A guessed
rectangle rejects real plates, which is worse than the echo it prevents. So
this script does not guess: it pulls real images and leaves them on disk.

Then, on the VA side, run the detector over them:

    python tools/measure_overlay_regions.py ./hik-samples

That prints where the boxes actually land and proposes a region.

Read-only against the platform: it downloads and writes files locally, and
consumes nothing. No HikValidation row, no GUID spent — that table's guid
column doubles as the reconciliation watermark, and a write here would advance
it and make the legacy reconciler skip real records.

Usage:
    python scripts/setup/probe_hik_images.py --hours 6 --limit 20 --out ./hik-samples
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


async def pull(hours: float, limit: int, out: Path) -> int:
    end = facility_now_naive()
    begin = end - timedelta(hours=hours)
    resource_ids = settings.hik_entry_resource_ids()
    if not resource_ids:
        print("HIK_ENTRY_RESOURCE_IDS is unset — nothing to query.")
        return 2

    print(f"Window : {begin} .. {end}  ({hours}h)")
    print(f"Camera : {resource_ids}")

    records = await client.query_vehicle_logs(
        begin=from_facility_naive(begin),
        end=from_facility_naive(end),
        resource_ids=resource_ids,
        page_size=max(limit, 50),
    )
    print(f"Records: {len(records)}")
    if not records:
        print(
            "\nNothing came back. Either the window is quiet or the indexCode is\n"
            "wrong — and an unknown code answers 200/code=0/empty exactly like a\n"
            "quiet camera. Widen --hours before concluding anything."
        )
        return 1

    out.mkdir(parents=True, exist_ok=True)
    saved = with_image = 0
    for record in records[:limit]:
        url = record.vehicle_image_url
        if not url:
            continue
        with_image += 1
        content = await client.download_picture(url)
        if content is None:
            continue
        target = out / f"hik_{record.guid or saved}.jpg"
        target.write_bytes(content)
        saved += 1
        print(f"  saved {target.name}  ({len(content)} bytes)  plate={record.plate}")

    print(f"\n{saved} image(s) saved to {out}")
    print(f"{with_image} of {min(limit, len(records))} record(s) carried an image.")
    if not saved:
        print(
            "\nNo images. Without them there is nothing for Re-ID to associate,\n"
            "so a HikCentral record can supply a plate reading but can never be\n"
            "a witness — and the overlay guard has nothing to measure."
        )
        return 1

    print(
        "\nNext, on the VA side:\n"
        f"    python tools/measure_overlay_regions.py {out}\n"
        "\nLook at a few by eye as well. You are checking two things: WHERE the\n"
        "composited panel sits, and whether the car is framed tightly enough for\n"
        "Re-ID to say anything useful about it."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=6.0)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, default=Path("./hik-samples"))
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
            return await pull(args.hours, args.limit, args.out)
        finally:
            await client.close_hikcentral_http_client()

    return asyncio.run(run())


if __name__ == "__main__":
    raise SystemExit(main())
