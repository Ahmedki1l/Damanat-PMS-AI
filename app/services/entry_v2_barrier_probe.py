"""Entry V3 shadow probe: did the BARRIER actually open for this car?

WHAT THIS ANSWERS. Entry V3 decides which identity owns a ramp crossing by
appearance. Over 2026-09-06..07 that produced two phantom entries and three
missed ones, and in 72 of 81 evaluations there was exactly ONE candidate — Re-ID
was choosing the only item on the list. Every failure sat in the other nine.

Ordering would settle those nine, but only over a queue of cars that actually
went through. Building that queue needs one fact this service has never had:
whether the barrier opened. `/artemis/api/pms/v1/crossRecords/page`, the only
entry API we call, does not carry it — its nine fields are GUID, PassTime,
PlateLicense, VehicleImageUrl, PlateImageUrl, ResourceID, ResourceName,
VehicleDirectionType, VehicleType, and none of them is barrier state.

Section 5.8.7 (p444) of the HikCentral Professional OpenAPI V3.0.0 guide does:

    POST /artemis/api/vehicle/v1/parkinglot/passageway/record
      allowResult   1 = allowed,  2 = NOT allowed
      allowType     1 = manual,   2 = auto,  3 = not allowed

`allowType` earns its place alongside `allowResult`: 1 (manual) means security
opened the barrier by hand, which is exactly the car that sat at the gate long
enough to blow the 60s window. It names the slow car from the record, instead of
asking Re-ID to rescue it.

WHAT WE DO NOT KNOW TODAY, and why this is a probe rather than a decision input.
Nothing in this service knows barrier state. The existing
`[Hik] entry REFUSED by the crossing gate` line is PMS-AI CONCLUDING a refusal
from its own 60s no-ramp-confirmation timeout and then tombstoning the matching
pass — HikCentral never reported one. So "burst dropped" and "REFUSED" correlate
four seconds apart only because one causes the other; that is circular and says
nothing about the barrier.

Two things therefore remain unverified until this probe has run against the real
platform, and NEITHER may be assumed:

  * `/artemis/api/vehicle/v1/*` may not be authorized for this partner at all,
    in which case every call returns code 69 and this module reports nothing.
  * The fields may be present in the schema and never populated in this
    deployment.

So the verdict is carried into the decision log as EVIDENCE TO REVIEW, next to
the Re-ID score that actually made the call. Nothing branches on it. When a day
of `entry_decisions_gate_*.jsonl` shows the verdict is populated and agrees with
what physically happened, that is the point at which an ordering design becomes
buildable — and not before.

DEGRADES, NEVER RAISES. An unauthorized namespace, a timeout, a malformed
payload and an empty window all return None. A raising config validator for an
optional add-on crash-looped every pod in this facility once already; an
add-on that cannot answer must be silent, never fatal.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from app.config import settings
from app.utils.logger import get_logger
from app.services import hikcentral
from app.services.hikcentral import client as hik_client

logger = get_logger(__name__)

PASSAGE_PATH = "/artemis/api/vehicle/v1/parkinglot/passageway/record"

# How far either side of the gate read to look. The measured barrier-to-ramp
# transit in this facility is 4-40s, and a manually admitted car is slower
# still, so the window is deliberately wider than the transit rather than
# tuned to it — we are identifying a pass, not timing one.
_WINDOW = timedelta(minutes=3)

# Said once per process. An unauthorized namespace is a configuration fact, not
# a per-car event, and 60-odd copies of it a day would bury the decision log.
_reported_unavailable = False


def _plate_key(value: Any) -> str:
    return "".join(
        character
        for character in str(value or "").upper()
        if character.isalnum()
    )


async def lookup_barrier_verdict(
    *,
    plate: str,
    event_time: datetime,
) -> Optional[dict]:
    """What the barrier did for this plate around this time, or None.

    None means "we could not find out" — unauthorized, unreachable, malformed,
    or simply no passage record in the window. It NEVER means "not allowed";
    that is `allow_result: 2`, and the difference is the whole point of the
    probe. A caller that treats None as a refusal would reinvent the circular
    inference this module exists to replace.
    """
    global _reported_unavailable

    if not settings.ENTRY_V2_BARRIER_PROBE_ENABLED:
        return None
    if settings.ENTRY_V2_MODE == "off":
        return None
    if not hikcentral.is_enabled():
        return None
    lot = (settings.HIK_PARKING_LOT_INDEX_CODE or "").strip()
    if not lot:
        if not _reported_unavailable:
            _reported_unavailable = True
            logger.info(
                "[EntryV2][barrier] probe idle: HIK_PARKING_LOT_INDEX_CODE is "
                "unset, so there is no lot to query. Run "
                "scripts/setup/probe_hik_barrier_result.py --list-lots to find "
                "it."
            )
        return None

    body = {
        "pageIndex": 1,
        "pageSize": 20,
        "queryInfo": {
            "parkingLotIndexCode": lot,
            "beginTime": (event_time - _WINDOW).isoformat(),
            "endTime": (event_time + _WINDOW).isoformat(),
            "directionType": 1,  # entries only
            "allowResult": -1,  # BOTH allowed and refused; seeing the
            # refusals is half of what we came for
            "sortField": "EnterTime",
            "orderType": 0,
        },
    }

    try:
        response = await hik_client._signed_post(PASSAGE_PATH, body)
    except Exception as exc:  # pragma: no cover - client already fails soft
        logger.debug("[EntryV2][barrier] probe call failed: %r", exc)
        return None
    if response is None:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None

    code = hik_client.response_code(payload)
    if code != "0":
        if not _reported_unavailable:
            _reported_unavailable = True
            detail = (
                "the /artemis/api/vehicle/v1/* namespace is not authorized for "
                "this partner (System -> Third-Party Integration -> OpenAPI "
                "Gateway -> API Management)"
                if code == "69"
                else "the platform refused the call"
            )
            logger.info(
                "[EntryV2][barrier] probe unavailable: code=%s — %s. Barrier "
                "verdicts will be absent from the decision log; that is not a "
                "refusal, it is a missing answer.",
                code,
                detail,
            )
        return None

    rows = ((payload.get("data") or {}).get("list")) or []
    if not rows:
        return None

    # Match on the plate, then take the record nearest the gate read. Nearest
    # alone is not identity — a car can wait on the ramp — so the plate is the
    # filter and proximity only breaks ties among that car's own passes.
    wanted = _plate_key(plate)
    best = None
    best_gap = None
    for row in rows:
        car = row.get("carInfo") or {}
        if wanted and _plate_key(car.get("plateLicense")) != wanted:
            continue
        gap = _enter_gap(car.get("EnterTime"), event_time)
        if gap is None:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = row, gap
    if best is None:
        return None

    car = best.get("carInfo") or {}
    return {
        "guid": str(best.get("guid") or ""),
        "plate_license": str(car.get("plateLicense") or ""),
        "enter_time": str(car.get("EnterTime") or ""),
        # Reported RAW, exactly as the platform spelled them. A probe that
        # normalises its own evidence cannot be used to check the platform.
        "allow_result": best.get("allowResult"),
        "allow_type": best.get("allowType"),
        "gap_seconds": round(best_gap, 1),
        "records_in_window": len(rows),
    }


def _enter_gap(enter_time: Any, event_time: datetime) -> Optional[float]:
    """Absolute seconds between a record's EnterTime and our gate read."""
    if not enter_time:
        return None
    try:
        parsed = datetime.fromisoformat(str(enter_time))
    except ValueError:
        return None
    try:
        return abs((parsed - event_time).total_seconds())
    except TypeError:
        # Mixed aware/naive. The probe reports nothing rather than a number it
        # cannot justify.
        return None
