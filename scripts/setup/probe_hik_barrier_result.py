"""Probe: does HikCentral tell us whether the BARRIER actually opened?

WHY THIS EXISTS. Entry V3 decides which identity owns a ramp crossing by
appearance. Across 2026-09-06..07 that produced two phantom entries and three
missed ones, and in 72 of 81 evaluations there was exactly ONE candidate — Re-ID
was picking the only item on the list. Every failure was in the other nine.

The signal that would remove the ambiguity is which cars physically went
through, in order. `/artemis/api/pms/v1/crossRecords/page` — the ONLY entry API
this service calls — cannot answer that. Its nine parsed fields (GUID, PassTime,
PlateLicense, VehicleImageUrl, PlateImageUrl, ResourceID, ResourceName,
VehicleDirectionType, VehicleType) carry no barrier state at all.

An entire namespace does answer it, and nothing here calls it. Per the
HikCentral Professional OpenAPI V3.0.0 guide, section 5.8.7 (p444):

    POST /artemis/api/vehicle/v1/parkinglot/passageway/record

      carInfo     { plateLicense, carType, ImageUrl, EnterTime, ExitTime }
      allowResult   1 = allowed,  2 = NOT allowed
      allowType     1 = manual,   2 = auto,  3 = not allowed

`allowType` matters as much as `allowResult`: 1 (manual) means security opened
the barrier by hand, which is precisely the car that sat at the gate long enough
to blow the 60s window. It names the slow car from the record instead of asking
Re-ID to rescue it.

WHAT WE DO NOT KNOW TODAY. Nothing in this service knows barrier state. The
`[Hik] entry REFUSED by the crossing gate` line is PMS-AI CONCLUDING a refusal
from its own 60s no-ramp-confirmation timeout and then tombstoning the matching
pass — HikCentral never reported one. So "burst dropped" and "REFUSED" always
correlate 4 seconds apart because one causes the other. That correlation is
circular and proves nothing.

READ THE ANSWER BY CONTENT, NEVER BY ABSENCE OF ERROR.

    An unauthorized namespace and an empty window both return HTTP 200.

Only the top-level `code` separates them: "0" is success, "69" is UnAuthorized.
This is the same trap that let HIK_EXIT_RESOURCE_IDS point at a camera that does
not exist while the exit reconciler swept happily for months. So this script
never says "no records" without first proving the call was authorized.

THE THREE QUESTIONS IT ANSWERS, in one run:

    1. Is /artemis/api/vehicle/v1/* authorized for this partner at all?
       (If not: System -> Third-Party Integration -> OpenAPI Gateway ->
       API Management, or every call returns code 69.)
    2. Are allowResult / allowType actually POPULATED in this deployment,
       or present-but-always-null?
    3. Does a 7-second tailgate produce ONE passage record or TWO?

Question 3 is the one that decides whether an ordering design is safe. The
default window is the recorded KXR-2538 / RDJ-9640 pair from 2026-09-07, whose
barrier passes were 09:10:37 and 09:10:44 — seven seconds apart.

Read-only. It queries, prints, and writes nothing.

Usage:
    # 1. Which parking lots does the platform know about?
    python scripts/setup/probe_hik_barrier_result.py --list-lots

    # 2. The recorded tailgate pair (default window).
    python scripts/setup/probe_hik_barrier_result.py --lot <indexCode>

    # 3. Any other window, facility local time.
    python scripts/setup/probe_hik_barrier_result.py --lot <c> \
        --from 2026-09-07T09:00:00 --to 2026-09-07T10:00:00
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.config import settings  # noqa: E402
from app.services.hikcentral import client  # noqa: E402

LOT_LIST_PATH = "/artemis/api/vehicle/v1/parkinglot/list"
PASSAGE_PATH = "/artemis/api/vehicle/v1/parkinglot/passageway/record"

# The recorded tailgate: KXR-2538 passed at 09:10:37 and RDJ-9640 at 09:10:44
# on 2026-09-07, and V3 received one CAM-23 crop for each. Whether the platform
# logged one passage or two is the whole question.
DEFAULT_FROM = "2026-09-07T09:10:00"
DEFAULT_TO = "2026-09-07T09:11:30"

ALLOW_RESULT = {1: "ALLOWED", 2: "NOT ALLOWED"}
ALLOW_TYPE = {1: "manual (security opened it)", 2: "auto", 3: "not allowed"}


def _offset() -> str:
    """The facility's UTC offset, formatted for ISO 8601 as the API wants it."""
    hours = float(getattr(settings, "FACILITY_TIMEZONE_OFFSET_HOURS", 3.0) or 0.0)
    sign = "+" if hours >= 0 else "-"
    hours = abs(hours)
    return f"{sign}{int(hours):02d}:{int(round((hours % 1) * 60)):02d}"


def _iso(local_naive: str) -> str:
    """'2026-09-07T09:10:00' -> '2026-09-07T09:10:00+03:00'."""
    datetime.fromisoformat(local_naive)  # fail loudly on a malformed argument
    return f"{local_naive}{_offset()}"


async def _call(path: str, body: dict) -> tuple[str | None, dict]:
    """One signed POST. Returns (top-level code, payload)."""
    response = await client._signed_post(path, body)
    if response is None:
        return None, {}
    try:
        payload = response.json()
    except ValueError:
        print(f"  {path}: non-JSON response (HTTP {response.status_code})")
        return None, {}
    return client.response_code(payload), payload


def _explain_code(code: str | None, path: str) -> bool:
    """True when the call was authorized and succeeded. Never guesses."""
    if code is None:
        print(f"  UNREACHABLE  {path} — no usable response (transport or auth).")
        return False
    if code == "0":
        return True
    if code == "69":
        print(f"  UNAUTHORIZED {path} — code 69.")
        print("               This namespace is not enabled for the ParkingAI")
        print("               partner. System -> Third-Party Integration ->")
        print("               OpenAPI Gateway -> API Management. Until that is")
        print("               done every call here returns 69, and an empty")
        print("               result means NOTHING about the barrier.")
        return False
    print(f"  REFUSED      {path} — code={code}")
    return False


async def list_lots() -> None:
    print(f"\nAsking {LOT_LIST_PATH} ...")
    code, payload = await _call(LOT_LIST_PATH, {})
    if not _explain_code(code, LOT_LIST_PATH):
        return
    rows = ((payload.get("data") or {}).get("list")) or []
    if not rows:
        print("  AUTHORIZED but the platform reports NO parking lots.")
        return
    print(f"  AUTHORIZED — {len(rows)} parking lot(s):\n")
    for row in rows:
        print(
            f"    parkingLotIndexCode={row.get('parkingLotIndexCode')!r:<12} "
            f"name={row.get('parkingLotName')!r}"
        )
    print("\n  Re-run with --lot <parkingLotIndexCode> to read passage records.")


async def passage_records(lot: str, begin: str, end: str, raw: bool) -> None:
    body = {
        "pageIndex": 1,
        "pageSize": 100,
        "queryInfo": {
            "parkingLotIndexCode": lot,
            "beginTime": _iso(begin),
            "endTime": _iso(end),
            # -1 is "all" for both. We deliberately do NOT filter to allowed
            # here: seeing the refusals is half the point.
            "directionType": 1,
            "allowResult": -1,
            "sortField": "EnterTime",
            "orderType": 0,
        },
    }
    print(f"\nAsking {PASSAGE_PATH}")
    print(f"  lot={lot}  {body['queryInfo']['beginTime']} .. "
          f"{body['queryInfo']['endTime']}  (entries only)")
    code, payload = await _call(PASSAGE_PATH, body)
    if not _explain_code(code, PASSAGE_PATH):
        return

    data = payload.get("data") or {}
    rows = data.get("list") or []
    print(f"\n  AUTHORIZED — total={data.get('total')} returned={len(rows)}")
    if not rows:
        print("  The call succeeded and the window held NO passage records.")
        print("  That is a real answer now: the namespace works, so an empty")
        print("  window means the platform logged nothing here.")
        return

    if raw:
        print("\n--- raw ---")
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        print("--- end raw ---\n")

    populated_result = populated_type = 0
    print()
    for row in rows:
        car = row.get("carInfo") or {}
        allow_result = row.get("allowResult")
        allow_type = row.get("allowType")
        populated_result += allow_result is not None
        populated_type += allow_type is not None
        print(
            f"    {str(car.get('EnterTime') or '-'):<28} "
            f"{str(car.get('plateLicense') or '-'):<12} "
            f"allowResult={allow_result!s:<5} "
            f"({ALLOW_RESULT.get(allow_result, 'UNKNOWN')})  "
            f"allowType={allow_type!s:<5} "
            f"({ALLOW_TYPE.get(allow_type, 'UNKNOWN')})"
        )
        print(f"      guid={row.get('guid')}")

    print(f"\n  allowResult populated on {populated_result}/{len(rows)} rows")
    print(f"  allowType   populated on {populated_type}/{len(rows)} rows")
    if not populated_result:
        print("  PRESENT BUT EMPTY — the field exists in the schema and this")
        print("  deployment never fills it. An ordering design cannot rely on")
        print("  it; say so before building on it.")
    print(
        f"\n  {len(rows)} passage record(s) in this window. Compare against the "
        "CAM-23\n  line events for the same window: one per vehicle. If CAM-23 "
        "fired more\n  times than there are records here, a car came through on "
        "somebody else's\n  barrier opening — which must ABSTAIN and alert, "
        "never shift the queue."
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list-lots", action="store_true")
    parser.add_argument("--lot", help="parkingLotIndexCode from --list-lots")
    parser.add_argument("--from", dest="begin", default=DEFAULT_FROM,
                        help="facility local time, e.g. 2026-09-07T09:10:00")
    parser.add_argument("--to", dest="end", default=DEFAULT_TO)
    parser.add_argument("--raw", action="store_true",
                        help="dump the raw JSON rows as well")
    args = parser.parse_args()

    print(f"HikCentral base_url={settings.HIK_BASE_URL} "
          f"appKey={getattr(settings, 'HIK_APP_KEY', '?')}")

    if args.list_lots or not args.lot:
        await list_lots()
        if not args.lot:
            return
    await passage_records(args.lot, args.begin, args.end, args.raw)


if __name__ == "__main__":
    asyncio.run(main())
