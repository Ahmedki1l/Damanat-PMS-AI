"""Entry V2 stage S2 — after an ANPR read, WE query HikCentral.

DIRECTION OF COMMUNICATION. HikCentral is a PULL source. It does not push entry
logs at us and it never triggers this pipeline. Our service holds an event — a
gate ANPR read — and goes and asks HikCentral what it has around that moment:

    ANPR event arrives
          |
    OUR SERVICE calls the HikCentral API
          |
    HikCentral returns candidate records
          |
    we download the vehicle images it points at
          |
    we forward them to VA as evidence, and VA's Re-ID decides
    which candidate (if any) is actually this car

WHAT THIS IS NOT. It is not `validate_entry_plate`. That function picks the
record whose PassTime is nearest the event and hands back one answer, and it is
on the legacy authoritative burst-flush path that still runs production. Two
reasons it is untouched here:

  * Nearest-timestamp is choosing a vehicle by proximity. A car can stop, wait
    or be delayed on the ramp, so proximity is not identity. Every record we get
    back is a CANDIDATE; Re-ID over the images decides.
  * Changing it would alter live behaviour behind the shadow gate, and nothing
    in this work may touch production state before the review passes.

WHAT IT CONSUMES. Nothing. No HikValidation row is written and no GUID is spent.
`HikValidation.guid` doubles as the reconciliation watermark (MAX(pass_time) per
direction), so consuming a GUID here would advance that watermark and make the
legacy reconciler skip real records — silently damaging the very entry recovery
this work exists to improve. That is the most dangerous leak in this design and
it is closed by simply never writing.
"""
from __future__ import annotations

import json

from datetime import datetime
from typing import Optional
from uuid import uuid5

import httpx

from app.config import settings
from app.utils.logger import get_logger
from app.services import hikcentral
from app.services.hikcentral import client as hik_client
from app.services.entry_v2_forwarder import (
    _ID_NAMESPACE,
    _MODE_HEADER,
    _post_entry_v2,
    _semantic_ack_error,
)

logger = get_logger(__name__)

# How many candidate images we are willing to fetch for one ANPR event. The
# window can legitimately hold several cars; this bounds the work without
# choosing between them.
DEFAULT_MAX_CANDIDATE_IMAGES = 5


async def enrich_entry_from_hikcentral(
    *,
    plate: str,
    event_time: datetime,
    camera_id: str,
    max_candidates: int = DEFAULT_MAX_CANDIDATE_IMAGES,
) -> dict:
    """Query HikCentral for one ANPR event and forward what it returns to VA.

    Returns the `hik` block for the decision log: what WE asked for and what
    came back. Never raises — a HikCentral outage is a degraded query, not a
    lost event, because there was no event of theirs to lose. The entry
    proceeds on ANPR and camera evidence.
    """
    block: dict = {
        # OUR event, never a HikCentral one. The plate is recorded as the
        # anchor we queried around, NOT as a filter: the query returns every
        # record in the window whatever its plate, because deciding which of
        # them is this car is Re-ID's job and not a string comparison's.
        "trigger": "anpr_identity",
        "anchor_plate": plate,
        "queried": False,
        "records": 0,
        "images": 0,
        "forwarded": [],
        "no_image": [],
        # Candidates withheld because HikCentral's licence would not normalise.
        # Counted rather than only logged: "1 image fetched, 0 forwarded" with
        # no machine-readable reason is the exact shape that let this lane fail
        # invisibly for five days.
        "no_plate": [],
        "api_error": None,
    }
    if settings.ENTRY_V2_MODE == "off":
        return block
    if not hikcentral.is_enabled():
        block["api_error"] = "hikcentral_disabled"
        return block

    try:
        records = await hikcentral.list_entry_candidates(event_time)
    except Exception as exc:  # pragma: no cover - the client already fails soft
        logger.warning("[EntryV2][Hik] candidate query failed: %r", exc)
        block["api_error"] = repr(exc)
        return block

    block["queried"] = True
    block["records"] = len(records)
    if not records:
        # 18B: nothing in the window. Not an error, and not a reason to stop.
        return block

    for record in records[:max_candidates]:
        guid = getattr(record, "guid", "") or ""
        image_url = getattr(record, "vehicle_image_url", "") or ""
        if not image_url:
            # 18C: the record exists but carries no image. Its plate is still a
            # source (HikCentral's own reading); it just cannot be Re-ID'd, so
            # it can never become a witness.
            block["no_image"].append(guid)
            continue
        content = await _download(image_url)
        if content is None:
            block["no_image"].append(guid)
            continue
        if not (getattr(record, "canonical_plate", "") or "").strip():
            # Checked before the image is counted so the block stays readable:
            # this candidate was never a forward that failed, it was one we
            # declined to make. See _forward_candidate for why the raw
            # digits-first licence is not an acceptable substitute.
            block["no_plate"].append(guid)
            continue
        block["images"] += 1
        delivered = await _forward_candidate(
            record=record,
            content=content,
            camera_id=camera_id,
            event_time=event_time,
        )
        if delivered:
            block["forwarded"].append(guid)

    return block


async def _download(image_url: str) -> Optional[bytes]:
    try:
        return await hik_client.download_picture(image_url)
    except Exception as exc:  # pragma: no cover - client fails soft already
        logger.warning("[EntryV2][Hik] image download failed: %r", exc)
        return None


async def _forward_candidate(
    *,
    record,
    content: bytes,
    camera_id: str,
    event_time: datetime,
) -> bool:
    """Send one HikCentral candidate to VA as an attempt.

    It goes in as an ordinary attempt so VA's existing plate-keyed find-or-
    create attaches it to the identity that already exists for this plate, and
    its appearance guard decides whether the image may enrich that identity or
    is a different car under the same plate.

    The metadata marker is what stops it being mistaken for a gate read:
    `evidence_source=hikcentral` makes VA record HikCentral's plate as
    HIK_TEXT rather than ANPR, and a HIK witness rather than an ANPR one. Those
    are different sources on different axes, and counting one platform's answer
    twice is exactly what the consensus rule exists to prevent.
    """
    guid = getattr(record, "guid", "") or ""
    # `list_entry_candidates` hands back VehicleLogRecord, which spells the plate
    # `plate_license` (HikCentral's own digits-first "4920HBR") and
    # `canonical_plate` (normalize_plate of it, letters-first "HBR-4920").
    # It has NO `.plate` — that attribute belongs to HikOutcome. Reading it
    # through getattr's default silently produced "" on every candidate, and VA
    # answers an empty reported_plate with 422 invalid_reported_plate, so this
    # lane delivered nothing from the day it shipped (hik_sourced was false on
    # all 158 identities across five days of decision logs).
    #
    # It must be the CANONICAL spelling. VA's find-or-create keys the identity
    # on plate_key(reported_plate), which strips punctuation but does NOT
    # reorder: "HBR4920" and "4920HBR" are different keys. Sending the raw
    # digits-first licence would attach this candidate to a SECOND identity
    # group instead of the one the gate read already created — a phantom, which
    # is worse than the 422 it replaces. So when normalisation cannot produce a
    # canonical spelling we forward nothing and say why.
    plate = (getattr(record, "canonical_plate", "") or "").strip()
    if not plate:
        logger.warning(
            "[EntryV2][Hik] candidate guid=%s dropped: no canonical plate for "
            "licence %r — forwarding the raw spelling would fork the identity",
            guid,
            getattr(record, "plate_license", ""),
        )
        return False
    source_event_id = f"hik:{guid}"
    attempt_id = str(uuid5(_ID_NAMESPACE, f"attempt:{source_event_id}"))
    metadata = {
        "evidence_source": "hikcentral",
        "hik_guid": guid,
        "hik_pass_time": _iso(getattr(record, "pass_time", None)),
        "anchor_camera_id": camera_id,
        "anchor_event_time": _iso(event_time),
    }
    data = {
        "attempt_id": attempt_id,
        "source_event_id": source_event_id,
        "camera_id": camera_id,
        "captured_at": _iso(getattr(record, "pass_time", None)) or _iso(event_time),
        "reported_plate": plate,
        "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    }
    files = [("images", (f"hik_{guid or 'candidate'}.jpg", content, "image/jpeg"))]
    url = f"{settings.PMS_API_URL.rstrip('/')}/api/v2/entry-attempts"

    try:
        response = await _post_entry_v2(
            url,
            data=data,
            files=files,
            headers={
                "X-Service-Key": settings.ENTRY_V2_SERVICE_KEY,
                _MODE_HEADER: settings.ENTRY_V2_MODE,
            },
        )
    except httpx.HTTPError as exc:
        logger.warning("[EntryV2][Hik] forward failed guid=%s: %r", guid, exc)
        return False

    if response.status_code not in (200, 201):
        logger.warning(
            "[EntryV2][Hik] VA rejected candidate guid=%s status=%s",
            guid,
            response.status_code,
        )
        return False
    ack_error = _semantic_ack_error(response, attempt_id, settings.ENTRY_V2_MODE)
    if ack_error:
        logger.warning(
            "[EntryV2][Hik] VA ack rejected guid=%s: %s", guid, ack_error
        )
        return False
    return True


def _iso(value) -> str:
    return value.isoformat() if isinstance(value, datetime) else ""
