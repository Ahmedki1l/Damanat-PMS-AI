"""Matching rules and mode policy for the HikCentral layer.

HikCentral is NOT the normal plate source — the ANPR camera already reports the
plate. This module does exactly two things:

  * validate a plate the camera reported (`validate_entry_plate`)
  * recover a plate the camera failed to report (`recover_entry_plate`)

Callers receive one canonical plate and never learn where it came from.
"""

from datetime import timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import facility_now_naive, settings
from app.models.hik_validation import HikValidation
from app.services.event_parser import normalize_plate, plate_digits_lost
from app.services.hikcentral import client
from app.services.hikcentral.models import (
    PLATE_SOURCE_EDGE_ANPR,
    PLATE_SOURCE_HIK_CONFIRMED,
    PLATE_SOURCE_HIK_CORRECTED,
    PLATE_SOURCE_HIK_POLLED,
    PLATE_SOURCE_HIK_RECOVERED,
    HikImages,
    HikOutcome,
    VehicleLogRecord,
    from_facility_naive,
    to_facility_naive,
)
from app.services.snapshot_service import to_public_snapshot_url
from app.utils.logger import get_logger

logger = get_logger(__name__)

DIRECTION_ENTRY = "entry"
DIRECTION_EXIT = "exit"

# `match_reason` for a pass the crossing gate refused. Stored so the audit trail
# distinguishes "we never saw this car" from "we saw it and decided it did not
# enter" — the reconciler must only ever act on the first.
REFUSED_NO_CROSSING = "edge_refused_no_crossing"


def _enabled() -> bool:
    return settings.HIK_VALIDATION_MODE != "off"


def _authoritative() -> bool:
    return settings.HIK_VALIDATION_MODE == "authoritative"


def _log_tag() -> str:
    return "[Hik][shadow]" if settings.HIK_VALIDATION_MODE == "shadow" else "[Hik]"


def guid_already_used(db: Optional[Session], guid: str) -> bool:
    """True when this HikCentral event already backs another gate event.

    The GUID is HikCentral's identity for one vehicle pass. Reusing one would
    let a single platform record justify two sessions, which is exactly the
    failure that keying on plate alone would produce.
    """
    if db is None or not guid:
        return False
    return (
        db.query(HikValidation.id).filter(HikValidation.guid == guid).first()
        is not None
    )


async def _lookup(anchor_naive, resource_ids: str) -> list[VehicleLogRecord]:
    """Run the single narrow VehicleLogs query around one gate event."""
    anchor = from_facility_naive(anchor_naive)
    begin = anchor - timedelta(seconds=settings.HIK_QUERY_LOOKBACK_SECONDS)
    end = anchor + timedelta(seconds=settings.HIK_QUERY_LOOKAHEAD_SECONDS)
    return await client.query_vehicle_logs(
        begin=begin,
        end=end,
        resource_ids=resource_ids,
        page_size=settings.HIK_QUERY_PAGE_SIZE,
    )


def _closest(records, anchor_naive) -> Optional[VehicleLogRecord]:
    """The record whose PassTime sits nearest the gate event."""
    if not records:
        return None
    return min(
        records,
        key=lambda r: abs(
            (to_facility_naive(r.pass_time) - anchor_naive).total_seconds()
        ),
    )


def _within_skew(record: VehicleLogRecord, anchor_naive) -> bool:
    delta = abs(
        (to_facility_naive(record.pass_time) - anchor_naive).total_seconds()
    )
    return delta <= settings.HIK_MATCH_MAX_SKEW_SECONDS


def _within_partial_skew(record: VehicleLogRecord, anchor_naive) -> bool:
    """The tighter window a record must also clear to complete a truncated plate.

    Never wider than the ordinary match skew, so this can only ever narrow the
    candidate set — a misconfigured value cannot admit a record that is not
    already an acceptable match.
    """
    limit = min(
        settings.HIK_PARTIAL_MATCH_MAX_SKEW_SECONDS,
        settings.HIK_MATCH_MAX_SKEW_SECONDS,
    )
    delta = abs(
        (to_facility_naive(record.pass_time) - anchor_naive).total_seconds()
    )
    return delta <= limit


async def _validate_plate(
    reported_plate: str,
    event_time,
    resource_ids: str,
    direction: str,
    db: Optional[Session] = None,
) -> HikOutcome:
    """Validate one camera-reported plate against HikCentral.

    Always returns an outcome whose `.plate` is the plate the caller must use.
    A disabled layer, an unreachable platform or an unmatched car all degrade to
    "keep the ANPR plate" — the pipeline behaves exactly as it did before.
    """
    canonical = normalize_plate(reported_plate) or reported_plate

    if not _enabled():
        return HikOutcome(
            plate=reported_plate,
            plate_source=PLATE_SOURCE_EDGE_ANPR,
            matched=False,
            reason="hik_disabled",
            reported_plate=reported_plate,
        )
    if not resource_ids:
        return HikOutcome(
            plate=reported_plate,
            plate_source=PLATE_SOURCE_EDGE_ANPR,
            matched=False,
            reason="no_resource_ids_configured",
            reported_plate=reported_plate,
        )

    records = await _lookup(event_time, resource_ids)
    if not records:
        logger.info(
            "%s %s no HikCentral record for plate=%s at %s",
            _log_tag(),
            direction,
            canonical,
            event_time,
        )
        return HikOutcome(
            plate=reported_plate,
            plate_source=PLATE_SOURCE_EDGE_ANPR,
            matched=False,
            reason="no_hik_record",
            reported_plate=reported_plate,
        )

    usable = [r for r in records if not guid_already_used(db, r.guid)]

    # Agreement alone does not mean the plate is right. This platform is fed by
    # the SAME entry LPR, so when the camera truncates a read it files that
    # truncation here too — and the lookup is plate-blind, so the fuller read of
    # the same car is usually sitting in `records` alongside it. Check for that
    # BEFORE accepting agreement, or the fuller record is never examined.
    # (2026-08-09: HikCentral held 6294KKR at 09:27:16 and 4KKR at 09:27:19, both
    # inside the skew window; this function confirmed KKR-4 and the correct row
    # was later tombstoned as refused.)
    fuller = [
        r
        for r in usable
        if _within_partial_skew(r, event_time)
        and plate_digits_lost(canonical, r.canonical_plate)
    ]
    # Completing a plate REWRITES it in authoritative mode, so it follows the
    # same discipline as `recover_entry_plate`: act only when the evidence names
    # exactly one car. Two different fuller plates in the window means two
    # candidates and nothing says which one this truncation came from.
    distinct_fuller = {r.canonical_plate for r in fuller}
    if len(distinct_fuller) > 1:
        logger.warning(
            "%s %s ambiguous partial plate=%s — %d candidates %s; keeping the "
            "ANPR plate",
            _log_tag(),
            direction,
            canonical,
            len(distinct_fuller),
            sorted(distinct_fuller),
        )
        fuller = []
    if fuller:
        match = _closest(fuller, event_time)
        if _authoritative():
            logger.warning(
                "[Hik] %s partial plate completed %s -> %s guid=%s",
                direction,
                canonical,
                match.canonical_plate,
                match.guid,
            )
            return HikOutcome(
                plate=match.canonical_plate,
                plate_source=PLATE_SOURCE_HIK_CORRECTED,
                matched=True,
                reason="plate_completed",
                record=match,
                reported_plate=reported_plate,
                candidates_considered=len(records),
            )
        logger.warning(
            "%s %s partial plate anpr=%s hik=%s guid=%s "
            "(shadow: keeping the ANPR plate)",
            _log_tag(),
            direction,
            canonical,
            match.canonical_plate,
            match.guid,
        )
        return HikOutcome(
            plate=reported_plate,
            plate_source=PLATE_SOURCE_EDGE_ANPR,
            matched=False,
            reason="plate_partial_shadow",
            record=match,
            reported_plate=reported_plate,
            candidates_considered=len(records),
        )

    agreeing = [
        r
        for r in usable
        if r.canonical_plate == canonical and _within_skew(r, event_time)
    ]

    if agreeing:
        match = _closest(agreeing, event_time)
        logger.info(
            "%s %s confirmed plate=%s guid=%s pass_time=%s",
            _log_tag(),
            direction,
            canonical,
            match.guid,
            match.pass_time,
        )
        return HikOutcome(
            plate=reported_plate,
            plate_source=PLATE_SOURCE_HIK_CONFIRMED,
            matched=True,
            reason="plate_confirmed",
            record=match,
            reported_plate=reported_plate,
            candidates_considered=len(records),
        )

    # No agreement. A record close enough in time, carrying a readable but
    # different plate, is a genuine disagreement — HikCentral is ground truth.
    disagreeing = [
        r for r in usable if r.canonical_plate and _within_skew(r, event_time)
    ]
    if disagreeing:
        match = _closest(disagreeing, event_time)
        if _authoritative():
            logger.warning(
                "[Hik] %s plate corrected %s -> %s guid=%s",
                direction,
                canonical,
                match.canonical_plate,
                match.guid,
            )
            return HikOutcome(
                plate=match.canonical_plate,
                plate_source=PLATE_SOURCE_HIK_CORRECTED,
                matched=True,
                reason="plate_corrected",
                record=match,
                reported_plate=reported_plate,
                candidates_considered=len(records),
            )
        logger.warning(
            "%s %s plate mismatch anpr=%s hik=%s guid=%s "
            "(shadow: keeping the ANPR plate)",
            _log_tag(),
            direction,
            canonical,
            match.canonical_plate,
            match.guid,
        )
        return HikOutcome(
            plate=reported_plate,
            plate_source=PLATE_SOURCE_EDGE_ANPR,
            matched=False,
            reason="plate_mismatch_shadow",
            record=match,
            reported_plate=reported_plate,
            candidates_considered=len(records),
        )

    logger.info(
        "%s %s no usable HikCentral record for plate=%s (%d in window)",
        _log_tag(),
        direction,
        canonical,
        len(records),
    )
    return HikOutcome(
        plate=reported_plate,
        plate_source=PLATE_SOURCE_EDGE_ANPR,
        matched=False,
        reason="no_matching_record",
        reported_plate=reported_plate,
        candidates_considered=len(records),
    )


async def validate_entry_plate(
    reported_plate: str, event_time, db: Optional[Session] = None
) -> HikOutcome:
    """Validate the plate the entry ANPR camera reported."""
    return await _validate_plate(
        reported_plate,
        event_time,
        settings.hik_entry_resource_ids(),
        DIRECTION_ENTRY,
        db,
    )


async def recover_entry_plate(
    crossing_time, source_cam: str, db: Optional[Session] = None
) -> Optional[HikOutcome]:
    """Recover a plate for a crossing the ANPR camera never labelled.

    Returns an outcome ONLY when a session should be created from it, so the
    caller stays a plain `if outcome: ... else: silent_entry`. Ambiguity, an
    unreachable platform, and shadow mode all return None.

    Recovery deliberately requires *exactly one* candidate in the window: with
    two cars in flight there is no evidence saying which one crossed, and
    guessing would attach a stranger's plate to the session.
    """
    if not _enabled():
        return None

    resource_ids = settings.hik_entry_resource_ids()
    if not resource_ids:
        return None

    records = await _lookup(crossing_time, resource_ids)
    # A record with no readable plate cannot recover anything.
    candidates = [
        r
        for r in records
        if r.canonical_plate and not guid_already_used(db, r.guid)
    ]

    if len(candidates) != 1:
        logger.info(
            "%s recovery declined for %s crossing at %s: %d candidate(s) "
            "in window (need exactly 1)",
            _log_tag(),
            source_cam,
            crossing_time,
            len(candidates),
        )
        return None

    match = candidates[0]
    if not _authoritative():
        logger.warning(
            "%s would recover plate=%s guid=%s for %s crossing at %s "
            "(shadow: no session created)",
            _log_tag(),
            match.canonical_plate,
            match.guid,
            source_cam,
            crossing_time,
        )
        return None

    logger.warning(
        "[Hik] recovered plate=%s guid=%s for %s crossing at %s "
        "(ANPR reported nothing)",
        match.canonical_plate,
        match.guid,
        source_cam,
        crossing_time,
    )
    return HikOutcome(
        plate=match.canonical_plate,
        plate_source=PLATE_SOURCE_HIK_RECOVERED,
        matched=True,
        reason="plate_recovered",
        record=match,
        reported_plate=None,
        candidates_considered=len(records),
    )


def is_enabled() -> bool:
    """Whether the HikCentral layer is on (shadow or authoritative)."""
    return _enabled()


def is_authoritative() -> bool:
    """Whether HikCentral may change behaviour (create/close sessions)."""
    return _authoritative()


def polled_outcome(record: VehicleLogRecord) -> HikOutcome:
    """Wrap a reconciliation-poller record as an actionable outcome.

    Distinct source (`hik_polled`) because this pass was never confirmed by any
    edge event — HikCentral's record is the sole evidence for the session.
    """
    return HikOutcome(
        plate=record.canonical_plate,
        plate_source=PLATE_SOURCE_HIK_POLLED,
        matched=True,
        reason="hik_polled_reconcile",
        record=record,
        reported_plate=None,
        candidates_considered=1,
    )


async def list_unconsumed_records(
    resource_ids: str,
    window_begin_naive,
    window_end_naive,
    db: Optional[Session],
) -> list[VehicleLogRecord]:
    """Recent HikCentral passes at one camera that no session has consumed yet.

    Used by the reconciliation poller to find gate events the edge pipeline
    never noticed. A record whose GUID is already in `hik_validations` was
    consumed by an earlier pass (or an earlier poll), so overlapping windows are
    idempotent. Returns [] when the layer is off or the camera is unconfigured.
    """
    if not _enabled() or not resource_ids:
        return []
    begin = from_facility_naive(window_begin_naive)
    end = from_facility_naive(window_end_naive)
    records = await client.query_vehicle_logs(
        begin, end, resource_ids, settings.HIK_RECONCILE_PAGE_SIZE
    )
    return [
        r
        for r in records
        if r.canonical_plate and not guid_already_used(db, r.guid)
    ]


async def consume_refused_entry(
    db: Optional[Session], reported_plate: str, event_time
) -> Optional[str]:
    """Consume the HikCentral record behind an entry the crossing gate REFUSED.

    The ANPR camera fired but no ramp crossing ever confirmed a car, so the edge
    concluded nothing entered and deliberately wrote no `EntryExitLog`. That is
    precisely the condition `_reconcile_missed_entries` reads as "the edge missed
    this car" — so without this, the reconciler re-opens every entry the gate
    refuses, and the crossing confirmation is decorative.

    Recording a `matched=False` row consumes the GUID, which is all it takes:
    `list_unconsumed_records` already filters consumed GUIDs, so the reconciler
    needs no knowledge of refusals and stays a pure "what did we never see?" sweep.

    Returns the consumed GUID, or None when there was nothing to consume — layer
    off, no HikCentral record in the window, or already consumed. All are safe:
    if HikCentral holds no record for this pass, the reconciler has nothing to
    act on either.
    """
    if not _enabled() or db is None:
        return None
    resource_ids = settings.hik_entry_resource_ids()
    if not resource_ids:
        return None

    try:
        records = await _lookup(event_time, resource_ids)
    except Exception as exc:  # a refusal must never break the flusher
        logger.warning(
            "[Hik] refused-entry lookup failed for plate=%s at %s: %r",
            reported_plate, event_time, exc,
        )
        return None

    usable = [
        r
        for r in records
        if r.canonical_plate
        and _within_skew(r, event_time)
        and not guid_already_used(db, r.guid)
    ]
    if not usable:
        logger.info(
            "%s entry refused for plate=%s at %s — no unconsumed HikCentral "
            "record to tombstone (nothing for the reconciler to re-open)",
            _log_tag(), reported_plate, event_time,
        )
        return None

    # Prefer the record agreeing with what the camera read; otherwise the closest
    # pass in the window — the same one the reconciler would otherwise act on.
    canonical = normalize_plate(reported_plate) or reported_plate
    agreeing = [r for r in usable if r.canonical_plate == canonical]
    match = _closest(agreeing or usable, event_time)

    db.add(
        HikValidation(
            session_id=None,
            entry_exit_log_id=None,
            direction=DIRECTION_ENTRY,
            guid=match.guid,
            plate_license=match.plate_license,
            canonical_plate=match.canonical_plate,
            reported_plate=reported_plate,
            # The ANPR camera is what reported this plate; HikCentral only
            # supplies the identity of the pass we are refusing.
            plate_source=PLATE_SOURCE_EDGE_ANPR,
            pass_time=to_facility_naive(match.pass_time),
            resource_id=match.resource_id,
            resource_name=match.resource_name,
            matched=False,
            match_reason=REFUSED_NO_CROSSING,
            created_at=facility_now_naive(),
        )
    )
    logger.warning(
        "[Hik] entry REFUSED by the crossing gate — consuming guid=%s plate=%s "
        "pass_time=%s so the reconciler cannot re-open it",
        match.guid, match.canonical_plate, to_facility_naive(match.pass_time),
    )
    return match.guid


async def download_hik_images(outcome: Optional[HikOutcome]) -> HikImages:
    """Fetch the vehicle and plate imagery for a matched record.

    Called only once a session is certain — never for an expired candidate, an
    ambiguous window or a burst suppressed as a duplicate.
    """
    if outcome is None or not outcome.has_evidence:
        return HikImages()

    record = outcome.record
    vehicle_public = plate_public = None
    vehicle_local = plate_local = None

    if record.vehicle_image_url:
        content = await client.download_picture(record.vehicle_image_url)
        vehicle_local = client.save_picture(content, record.guid, "vehicle")
        vehicle_public = to_public_snapshot_url(vehicle_local)
    if record.plate_image_url:
        content = await client.download_picture(record.plate_image_url)
        plate_local = client.save_picture(content, record.guid, "plate")
        plate_public = to_public_snapshot_url(plate_local)

    return HikImages(
        vehicle_image_path=vehicle_public,
        plate_image_path=plate_public,
        vehicle_local_path=vehicle_local,
        plate_local_path=plate_local,
    )


def record_hik_validation(
    db: Session,
    *,
    outcome: Optional[HikOutcome],
    direction: str,
    images: Optional[HikImages] = None,
    session_id: Optional[int] = None,
    entry_exit_log_id: Optional[int] = None,
) -> Optional[HikValidation]:
    """Persist the HikCentral evidence behind one gate event.

    Adds to the session without committing — the caller owns the transaction.
    Returns None when there is nothing worth storing.
    """
    if outcome is None or not outcome.has_evidence:
        return None
    if guid_already_used(db, outcome.record.guid):
        return None

    record = outcome.record
    images = images or HikImages()
    row = HikValidation(
        session_id=session_id,
        entry_exit_log_id=entry_exit_log_id,
        direction=direction,
        guid=record.guid,
        plate_license=record.plate_license,
        canonical_plate=record.canonical_plate,
        reported_plate=outcome.reported_plate,
        plate_source=outcome.plate_source,
        pass_time=to_facility_naive(record.pass_time),
        resource_id=record.resource_id,
        resource_name=record.resource_name,
        vehicle_image_path=images.vehicle_image_path,
        plate_image_path=images.plate_image_path,
        vehicle_type=record.vehicle_type,
        vehicle_direction_type=record.vehicle_direction_type,
        matched=outcome.matched,
        match_reason=outcome.reason,
        created_at=facility_now_naive(),
    )
    db.add(row)
    return row
