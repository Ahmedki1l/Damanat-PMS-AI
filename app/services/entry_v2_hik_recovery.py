"""Entry V2 stage S7 — a camera saw a car we cannot account for, so WE ask.

THE GAP THIS CLOSES. The ANPR camera drops a read. A car physically crosses the
ramp line and nothing at the gate ever named it, so no identity exists for the
observation to attach to. Downstream that reads as a silent entry, and days
later as a phantom overstay.

THE TRIGGER IS OURS. HikCentral has no idea we are missing an entry and cannot
tell us; it does not push and it does not initiate. What happens is that WE
notice a gap of our own — a camera observation with no identity — and go and
ask the platform what it holds:

    CAM-23 / CAM-03 observation
              |
        no ANPR identity
              |
    OUR SERVICE calls the HikCentral API
              |
    candidate records come back
              |
    Re-ID + our own OCR over the returned images

WHAT COUNTS AS A DROPPED READ. A VCA event on a ramp camera with NO ANPR
crossRecord anywhere near it. The event says something crossed the line; the
absent crossRecord says the gate never named it. Neither statement alone is
enough, which is why this correlates the two.

THE GATE ON ALL OF IT. `HIK_RAMP_RESOURCE_IDS` is empty until the probe
confirms real indexCodes, and an unknown code answers HTTP 200 / code=0 /
empty — indistinguishable from an idle camera. That is exactly how
HIK_EXIT_RESOURCE_IDS=453 swept for months against a camera that did not exist
and never closed an exit. With no codes configured this module reports
`configured: False` and does nothing, rather than reporting a confident zero.

CONSUMES NOTHING. Same rule as the S2 enrichment: no HikValidation row, no GUID
spent. That table's guid column doubles as the reconciliation watermark, so a
write here would advance it and make the legacy reconciler skip real records.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from app.config import settings
from app.services import hikcentral
from app.services.hikcentral import client as hik_client
from app.services.hikcentral.models import from_facility_naive
from app.utils.logger import get_logger

logger = get_logger(__name__)

# How far either side of the observation to look. Wide enough to cover a car
# that waited on the ramp, bounded so a sweep cannot walk unbounded history.
DEFAULT_WINDOW_SECONDS = 180

# How close an ANPR crossRecord has to be for the gate to count as having named
# this car. A PURELY NEGATIVE test: it decides whether a read EXISTS, never
# which car it belongs to. Nothing here is allowed to conclude identity from a
# timestamp — that is Re-ID's job, over the images these records point at.
DEFAULT_ANPR_PRESENCE_SECONDS = 90


async def find_dropped_gate_reads(
    *,
    observed_at: datetime,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
    anpr_presence_seconds: int = DEFAULT_ANPR_PRESENCE_SECONDS,
) -> dict:
    """Ask HikCentral what it holds around an unexplained camera observation.

    Returns the `hik` block for the decision log. Never raises: an unreachable
    platform is a degraded query, not a lost event, because there was no event
    of theirs to lose.
    """
    block: dict = {
        "trigger": "missing_anpr_recovery",
        "configured": bool(settings.HIK_RAMP_RESOURCE_IDS),
        "queried": False,
        "events": 0,
        "anpr_records": 0,
        "dropped": [],
        "explained": [],
        "no_image": [],
        "api_error": None,
    }
    if settings.ENTRY_V2_MODE == "off" or not hikcentral.is_enabled():
        return block
    if not settings.HIK_RAMP_RESOURCE_IDS:
        # Not "no events" — we never asked. Reporting a confident zero here is
        # the exact shape of the 453 incident, so say what actually happened.
        block["api_error"] = "hik_ramp_resource_ids_unset"
        return block

    begin = observed_at - timedelta(seconds=window_seconds)
    end = observed_at + timedelta(seconds=window_seconds)

    try:
        events = await hik_client.list_camera_events(
            begin=from_facility_naive(begin),
            end=from_facility_naive(end),
            resource_ids=settings.HIK_RAMP_RESOURCE_IDS,
            page_size=settings.HIK_RECONCILE_PAGE_SIZE,
        )
        anpr_records = await hikcentral.list_entry_candidates(observed_at)
    except Exception as exc:  # pragma: no cover - the client already fails soft
        logger.warning("[EntryV2][Hik] recovery query failed: %r", exc)
        block["api_error"] = repr(exc)
        return block

    block["queried"] = True
    block["events"] = len(events)
    block["anpr_records"] = len(anpr_records)

    for event in events:
        if event.start_time is None:
            # Without a time there is no way to say whether the gate named this
            # crossing, so it cannot be called a dropped read.
            block["explained"].append(event.event_index_code)
            continue
        if _gate_named_something_near(
            event.start_time, anpr_records, anpr_presence_seconds
        ):
            # The gate DID read a plate around this crossing. Which car that
            # read belongs to is not decided here — only that the gate was not
            # silent, so this is not the dropped-read case.
            block["explained"].append(event.event_index_code)
            continue
        if not event.event_pic_uri:
            # A dropped read we cannot act on: with no image there is nothing
            # for Re-ID to associate, so it can never become a witness. Worth
            # recording — it is evidence the recovery path is being starved.
            block["no_image"].append(event.event_index_code)
            continue
        block["dropped"].append(event.event_index_code)

    if block["dropped"]:
        logger.info(
            "[EntryV2][Hik] %d ramp event(s) with no gate read near them "
            "(window=%ss)",
            len(block["dropped"]),
            window_seconds,
        )
    return block


def _gate_named_something_near(
    event_time: datetime,
    anpr_records,
    presence_seconds: int,
) -> bool:
    """Did the ANPR gate produce ANY read close to this crossing?

    Deliberately a presence test and nothing more. It answers "was the gate
    silent?", which is a question about our own coverage; it never answers
    "which car is this?", which is a question only Re-ID may answer. Time is
    scoping a search, exactly as it does everywhere else in this pipeline.
    """
    from app.services.hikcentral.models import to_facility_naive

    for record in anpr_records:
        pass_time = getattr(record, "pass_time", None)
        if pass_time is None:
            continue
        try:
            delta = abs((to_facility_naive(pass_time) - event_time).total_seconds())
        except TypeError:
            continue
        if delta <= presence_seconds:
            return True
    return False


async def recover_images_for(event_index_codes, events) -> dict:
    """Download the images behind the events named as dropped reads.

    Returned so the caller can forward them to VA as evidence. Which of them is
    the observed car — if any — is decided there, by Re-ID. Several cars may
    legitimately have crossed in the window, and an event that does not
    associate is simply another vehicle passing through.
    """
    wanted = set(event_index_codes or ())
    images: dict[str, bytes] = {}
    for event in events or ():
        if event.event_index_code not in wanted or not event.event_pic_uri:
            continue
        content = await _download(event.event_pic_uri)
        if content is not None:
            images[event.event_index_code] = content
    return images


async def _download(url: str) -> Optional[bytes]:
    try:
        return await hik_client.download_picture(url)
    except Exception as exc:  # pragma: no cover - client fails soft already
        logger.warning("[EntryV2][Hik] recovery image download failed: %r", exc)
        return None
