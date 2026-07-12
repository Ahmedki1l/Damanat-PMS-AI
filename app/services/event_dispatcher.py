# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

import time

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.entry_exit_service import handle_anpr_event
from app.services.camera_feed_service import add_event_to_feed
from app.services.snapshot_service import fetch_snapshot
from app.utils.event_bus import event_bus
from app.utils.logger import get_logger
from app.config import settings
from sqlalchemy.orm import Session

logger = get_logger(__name__)


# ── ANPR gate-identity rescue ────────────────────────────────────────────────
# The entry LPR fires a `vehicleMatchResult` (which resolves to the gate camera)
# ~1-5s BEFORE the follow-on `ANPR`. On setups where every camera pushes through
# a gateway/NAT, that follow-on ANPR can arrive with an unresolvable identity
# (camera_id = "UNKNOWN-<gateway-ip>") because its body carries no mapped
# ipAddress/deviceSerial — handle_anpr_event then drops it for "no gate", so no
# entry is ever logged. We bridge the gap: remember the gate camera the paired
# vehicleMatchResult resolved to, keyed by plate, and let the follow-on ANPR
# inherit it. Bounded by ANPR_BURST_MAX_SECONDS so a stale hint can never
# mislabel a later car. Per-process, like the entry burst buffer it feeds.
_VMR_GATE_HINTS: dict[str, tuple[str, str, float]] = {}


def _remember_vmr_gate(event: ParsedCameraEvent) -> None:
    """Record plate → (camera_id, gate) when a vehicleMatchResult resolved to a
    known gate camera, so a follow-on UNKNOWN ANPR can inherit the identity."""
    gate = settings.CAMERAS.get(event.camera_id, {}).get("gate")
    if gate not in ("entry", "exit") or not event.plate_number:
        return
    now = time.monotonic()
    # Opportunistically prune expired hints so the dict stays bounded.
    for plate in [p for p, (_, _, dl) in _VMR_GATE_HINTS.items() if dl <= now]:
        _VMR_GATE_HINTS.pop(plate, None)
    _VMR_GATE_HINTS[event.plate_number] = (
        event.camera_id, gate, now + settings.ANPR_BURST_MAX_SECONDS,
    )


def _rescue_unknown_gate(event: ParsedCameraEvent) -> None:
    """If an ANPR resolved to an UNKNOWN camera but a recent vehicleMatchResult
    for the same plate resolved to a gate camera, adopt that identity so the
    entry/exit isn't dropped. Only rescues UNKNOWN cameras — never overrides an
    already-resolved one."""
    if not event.plate_number or not str(event.camera_id).startswith("UNKNOWN"):
        return
    hint = _VMR_GATE_HINTS.get(event.plate_number)
    if not hint:
        return
    camera_id, gate, deadline = hint
    if time.monotonic() > deadline:
        _VMR_GATE_HINTS.pop(event.plate_number, None)
        return
    logger.info(
        "[dispatch] Rescued ANPR %s → %s (gate=%s) via recent vehicleMatchResult "
        "for plate=%s", event.camera_id, camera_id, gate, event.plate_number,
    )
    event.camera_id = camera_id
    event.gate = gate

async def dispatch_event(event: ParsedCameraEvent, db: Session) -> dict:
    """
    Route event to handlers and commit as a single transaction.
    Protects UC3 and UC2 logic while maintaining teammates' UC5/UC6 work.

    Returns a dict of post-commit callbacks. The router must call these
    AFTER db.commit() succeeds (FIX #2: cache-vs-rollback mismatch).
    """
    # Collects cache keys that should only be recorded after successful commit
    _post_commit_cache_keys = []
    # Snapshot strategy:
    # 1. Use multipart image if the camera attached one to the event (already saved locally)
    # 2. If no image → try fetching a fresh snapshot from the camera (saves locally)
    # 3. If camera is unreachable → leave snapshot_path as None
    #
    # `vehicleMatchResult` is intentionally excluded: it arrives WITHOUT an
    # inline multipart image, which forces the ISAPI snapshot fetch path —
    # the path that 401s on entry cameras whose ISAPI ACL differs from the
    # exit camera's. Hikvision always fires a follow-on `ANPR` event ~1-5s
    # later that DOES carry the multipart JPEG, and that's what we use.
    # Treating both event types as canonical led to duplicate snapshot
    # attempts and the row being created from the imageless event first
    # (then dedup-suppressing the imageful event). See engine_dispatcher
    # commit history for the customer-prod RDJ-9640 case.
    SNAPSHOT_EVENT_TYPES = (
        "fielddetection", "linedetection", "regionEntrance",
        "AccessControllerEvent", "ANPR",
    )
    if event.event_type in SNAPSHOT_EVENT_TYPES:
        # event_parser.parse_camera_event already sets snapshot_path (URL) and
        # local_snapshot_path on multipart events. If neither is set, fall
        # back to fetching a fresh JPEG from the camera here.
        if not event.snapshot_path:
            try:
                fresh = await fetch_snapshot(event.camera_id, event.event_type)
                if fresh:
                    event.snapshot_path, event.local_snapshot_path = fresh
            except Exception as e:
                logger.warning(f"Snapshot fetch failed for {event.camera_id}: {e}")

    try:
        logger.debug(f"[dispatch] type={event.event_type!r} camera={event.camera_id} plate={event.plate_number!r}")

        # VMD = Video Motion Detection: basic motion, not a Smart Event.
        # Ignore it completely per user request — fires constantly and has no zone info.
        if event.event_type == "VMD":
            return

        # vehicleMatchResult: the imageless precursor to ANPR. We deliberately
        # don't process it here — see SNAPSHOT_EVENT_TYPES comment above. Just
        # log at DEBUG so we can confirm the camera fired it but we ignored it.
        if event.event_type == "vehicleMatchResult":
            # Not processed here, but remember which gate camera it resolved to so
            # the follow-on ANPR can inherit the identity if it arrives UNKNOWN.
            _remember_vmr_gate(event)
            logger.debug(
                "[dispatch] Skipping vehicleMatchResult from %s plate=%s — "
                "follow-on ANPR event will carry the multipart image",
                event.camera_id, event.plate_number,
            )
            return

        # Rescue an ANPR whose identity didn't resolve (UNKNOWN-<gateway-ip>) by
        # inheriting the gate from the paired vehicleMatchResult seen moments ago.
        # Done before the gate/feed logic below so the whole flow sees the real id.
        if event.event_type in ("ANPR", "AccessControllerEvent"):
            _rescue_unknown_gate(event)

        is_gate = settings.CAMERAS.get(event.camera_id, {}).get("gate") in ("entry", "exit")
        is_occupancy_event = event.event_type == "linedetection"
        should_log = False
    # Selective Logging: Only log gate events or smart events to reduce noise
        include = {x.strip() for x in settings.LOG_CAMERA_FILTER.split(",") if x.strip()} if settings.LOG_CAMERA_FILTER else set()
        exclude = {x.strip() for x in settings.LOG_CAMERA_EXCLUDE.split(",") if x.strip()} if settings.LOG_CAMERA_EXCLUDE else set()

        if include:
            should_log = event.camera_id in include
        elif exclude:
            should_log = event.camera_id not in exclude
        else:
            should_log = True

        if should_log and not is_gate and (is_occupancy_event or event.event_type in ("fielddetection", "linedetection", "regionEntrance")):
            logger.info(f"Event: type={event.event_type} | camera={event.camera_id} | plate={event.plate_number}")
            from app.services.alert_service import broadcast_event
            await broadcast_event(
                is_alert=False,
                severity="info",
                event_type=event.event_type,
                description=f"Live Event from {event.camera_id}",
                camera_id=event.camera_id,
                plate_number=event.plate_number,
                triggered_at=event.trigger_time,
                snapshot_path=event.snapshot_path
            )

        # ── CAMERA FEED ──────────────────────────────────────────────────────
        # Log all entry/exit related smart events to the CameraFeed table.
        # `vehicleMatchResult` excluded — ANPR follows ~1-5s later with the
        # same plate + an inline image, so it's the canonical row to log.
        FEED_EVENT_TYPES = ("ANPR", "AccessControllerEvent", "linedetection")
        if event.event_type in FEED_EVENT_TYPES:
            add_event_to_feed(db, event)

        # ── PHASE 1 ───────────────────────────────────────────────────────────
        
        # ✅ UC3: Parking Occupancy
        # Only line-crossing occupancy cameras are allowed to affect counts.
        is_occupancy_cam = event.camera_id in ("CAM-03", "CAM-08", "CAM-09", "CAM-10")
        is_smart_occupancy_event = is_occupancy_cam

        if is_smart_occupancy_event:
            # FIX #2: handle_occupancy_event now returns a cache_key (or None).
            # We collect it for post-commit recording instead of caching pre-commit.
            oc_cache_key = await handle_occupancy_event(event, db)
            if oc_cache_key:
                _post_commit_cache_keys.append(oc_cache_key)
        elif is_gate:
             logger.debug(f"[UC3] Gate camera {event.camera_id} sent non-occupancy event: {event.event_type}")

        # ── ENTRY RAMP CONFIRMATION ───────────────────────────────────────────
        # A ramp line-crossing cam (default CAM-23 — line-crossing only, no ANPR)
        # confirms one car physically entered → confirm the current ANPR burst
        # (and flags a silent entry if no plate was read). The set is configurable
        # via ENTRY_CONFIRM_CAMERAS; occupancy cameras in that set (e.g. CAM-03)
        # are excluded here because they confirm via occupancy_service — routing
        # them here too would double-confirm.
        entry_confirm_cams = {c.strip() for c in settings.ENTRY_CONFIRM_CAMERAS.split(",") if c.strip()}
        ramp_confirm_cams = entry_confirm_cams - {"CAM-03", "CAM-08", "CAM-09", "CAM-10"}
        if event.camera_id in ramp_confirm_cams and event.event_type == "linedetection":
            want_line = settings.CAM23_ENTRY_LINE.strip()
            want_dir = settings.CAM23_ENTRY_DIRECTION.strip()
            line_ok = (not want_line) or (str(event.region_id or "") == want_line)
            dir_ok = (not want_dir) or (str(event.crossing_direction or "") == want_dir)
            if line_ok and dir_ok:
                from app.services.entry_exit_service import confirm_entry_crossing
                await confirm_entry_crossing(
                    db,
                    snapshot=event.local_snapshot_path or event.snapshot_path,
                    source_cam=event.camera_id,
                )
            else:
                logger.debug(
                    f"[UC1] {event.camera_id} linedetection ignored (line={event.region_id!r} "
                    f"dir={event.crossing_direction!r}; want line={want_line!r} dir={want_dir!r})"
                )

        # ── PHASE 2 ───────────────────────────────────────────────────────────
        # UC1 + UC2 + UC4: ANPR gate events (JSON=AccessControllerEvent, XML=ANPR).
        # `vehicleMatchResult` is suppressed at the top of this function — only
        # ANPR (with multipart JPEG) drives entry_exit_log creation. This makes
        # the entry-camera flow identical to the exit-camera flow: both rely on
        # the inline multipart image, neither needs the ISAPI snapshot fetch.
        if event.event_type in ("ANPR", "AccessControllerEvent") and event.plate_number:
            await handle_anpr_event(event, db)

        # ── MAINTENANCE ───────────────────────────────────────────────────────
        # Pending logic removed per user request

        # FIX #2: Return pending cache keys for the router to record after commit
        return {"occupancy_cache_keys": _post_commit_cache_keys}

    except Exception as e:
        db.rollback()
        logger.error(f"Dispatch failed, transaction rolled back: {e}", exc_info=True)
        raise