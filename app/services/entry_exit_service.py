"""
Phase 2: UC1 (Entry/Exit Counting), UC2 (Parking Time), and UC4 (Vehicle ID).
Handles AccessControllerEvent / ANPR from ANPR cameras.

Note: `vehicleMatchResult` is intentionally NOT handled here. The dispatcher
suppresses it (see event_dispatcher.py) because Hikvision fires it without
an inline multipart image, which forces an ISAPI snapshot pull that some
camera ACL configs reject. The follow-on `ANPR` event (~1-5s later) carries
the multipart JPEG and drives row creation here. This makes entry-camera
behaviour identical to exit-camera behaviour: both rely on the inline image,
neither hits the ISAPI snapshot endpoint.

Entry burst aggregation (UC1)
-----------------------------
The entry ANPR camera fires SEVERAL reads for one car as it approaches the gate
(each `<picNum>` carries its own `<licensePlate>`/`<confidenceLevel>`). The early
read is often wrong (plate far/small/blurry); a LATER read is correct. We must
NOT label the car by the first read. So entry reads are buffered per entry
camera and only ONE entry is written — labeled by the LAST read of the burst —
after a short debounce window. The DB write, the PMS-API forward (port 8000) and
the vehicle-row resolution all happen ONCE, at flush time, on the winning plate.

A ramp line-crossing (CAM-23, the new entry-ramp cam — line-crossing only, no
ANPR — or CAM-03 deeper in the garage) physically confirms "one car entered":
it marks the current burst confirmed (the flusher then commits it) and acts as a
per-car boundary. A crossing with no buffered plate read is a SILENT ENTRY (the
ANPR missed the plate entirely) → an alert is raised.
"""

import asyncio
from datetime import timedelta
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.services import parking_session_service
from app.services import vehicle_service
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings, facility_now_naive, facility_tz
from app.utils.logger import get_logger
from app.utils import core_backend_client

logger = get_logger(__name__)

# ── ANPR entry-burst buffer ──────────────────────────────────────────────
# Keyed by an incrementing burst id (NOT plate, NOT camera) so a tailgater's
# burst can coexist with the previous car's not-yet-flushed burst. Each buffer:
#   {id, camera_id, reads[], first_event_time, last_read_at,
#    confirmed, confirm_snapshots{source:image}, confirm_source, force_flush}
# A read is {plate, confidence, pic_num, event_time, snapshot_path,
#            local_snapshot_path}.
_entry_bursts: dict[int, dict] = {}
_burst_seq = 0
# Ramp crossings (CAM-23/CAM-03) that arrived before any ANPR read — held briefly
# so a burst arriving just after still gets confirmed; otherwise → silent entry.
_pending_crossings: list[dict] = []
# Guards both structures. Handlers and the background flusher run on the same
# event loop; the lock is never held across network I/O (buffers are popped under
# the lock, then written outside it).
_bursts_lock = asyncio.Lock()

# Default direction marker for a confirmation snapshot forwarded to the PMS API.
# Distinct from the gate's "entry" so the PMS can tell a confirmation image apart
# from the ANPR gate image. Per-source markers come from ENTRY_CONFIRM_DIRECTIONS;
# this is the fallback (and CAM-03's historical marker).
CAM03_PMS_DIRECTION = "B-entry"

# Recently flushed entries, so a confirmation camera that fires AFTER the entry
# was written (CAM-03, deep in the garage) can still attach its image to the
# right car. Each item: {plate, ts, sent_sources:set}. Short-lived — pruned
# against ENTRY_CONFIRM_MATCH_SECONDS. Guarded by _bursts_lock.
_recent_entries: list[dict] = []


def _confirm_direction(source_cam: str) -> str:
    """Map a confirmation camera id → its PMS `direction` marker, parsed from
    ENTRY_CONFIRM_DIRECTIONS ("CAM-23:ramp-entry,CAM-03:B-entry"). Falls back to
    CAM03_PMS_DIRECTION for any source not listed."""
    for pair in settings.ENTRY_CONFIRM_DIRECTIONS.split(","):
        pair = pair.strip()
        if not pair or ":" not in pair:
            continue
        cam, _, direction = pair.partition(":")
        if cam.strip() == source_cam and direction.strip():
            return direction.strip()
    return CAM03_PMS_DIRECTION


def _new_burst(cam: str, event_time) -> dict:
    global _burst_seq
    _burst_seq += 1
    buf = {
        "id": _burst_seq,
        "camera_id": cam,
        "reads": [],
        "first_event_time": event_time,
        "last_read_at": facility_now_naive(),
        "confirmed": False,
        # source_cam → confirmation snapshot. A snapshot per confirming camera so
        # BOTH CAM-23 (ramp top) and a pre-flush CAM-03 (garage) can be forwarded.
        "confirm_snapshots": {},
        "confirm_source": None,   # first confirming source, for logging
        "force_flush": False,
    }
    _entry_bursts[buf["id"]] = buf
    return buf


def _open_buffer_for(cam: str) -> dict | None:
    """The single still-open (not yet closed for flush) burst for this camera."""
    for b in _entry_bursts.values():
        if b["camera_id"] == cam and not b["force_flush"]:
            return b
    return None


def _attach_pending_crossing(buf: dict) -> None:
    """If a ramp crossing arrived just before this burst started, consume the
    oldest still-valid one and mark the burst confirmed."""
    now = facility_now_naive()
    window = settings.ANPR_BURST_WINDOW_SECONDS
    _pending_crossings[:] = [
        c for c in _pending_crossings
        if (now - c["ts"]).total_seconds() <= window
    ]
    if _pending_crossings:
        c = _pending_crossings.pop(0)
        buf["confirmed"] = True
        src = c.get("source") or "CAM-23"
        if c.get("snapshot"):
            buf["confirm_snapshots"].setdefault(src, c["snapshot"])
        buf["confirm_source"] = src


async def _buffer_entry_read(event: ParsedCameraEvent, plate: str, event_time) -> None:
    """Append one ANPR read to the per-car entry burst. No DB write / no PMS
    forward here — those run once, at flush, on the winning (last) read."""
    async with _bursts_lock:
        cam = event.camera_id
        buf = _open_buffer_for(cam)
        pic = event.pic_num
        if buf and buf["reads"]:
            buffered_pics = [r["pic_num"] for r in buf["reads"] if r["pic_num"] is not None]
            max_pic = max(buffered_pics) if buffered_pics else None
            # Boundary: Hikvision restarts picNum at 1 per vehicle, so an incoming
            # pic_num <= one already buffered means the next car has begun. NOTE:
            # a crossing-confirmed burst is NOT a boundary — the CAM-23 line sits
            # at the ramp top next to the ANPR, so the BEST read arrives AFTER the
            # crossing (recognition lag) for the SAME car. Only picNum-reset (or
            # the idle gap, handled by the flusher) splits cars.
            picnum_reset = pic is not None and max_pic is not None and pic <= max_pic
            if picnum_reset:
                buf["force_flush"] = True   # close it; the flusher will write it
                buf = None
        if buf is None:
            buf = _new_burst(cam, event_time)
            _attach_pending_crossing(buf)
        buf["reads"].append({
            "plate": plate,
            "confidence": event.plate_confidence,
            "pic_num": pic,
            "event_time": event_time,
            "snapshot_path": event.snapshot_path,
            "local_snapshot_path": event.local_snapshot_path,
        })
        buf["last_read_at"] = facility_now_naive()
        logger.info(
            f"[UC1] Entry burst read buffered: cam={cam} plate={plate} "
            f"pic={pic} conf={event.plate_confidence} reads={len(buf['reads'])}"
        )


async def confirm_entry_crossing(db: Session, snapshot: str | None = None,
                                 source_cam: str = "CAM-23",
                                 allow_silent_entry: bool = True) -> None:
    """A ramp line-crossing physically confirms one car entered. Mark the most
    recent entry burst confirmed (the flusher then commits it) and stash the
    confirming snapshot.

    The CAM-23 line is drawn at the TOP of the ramp, right next to the ANPR cam,
    so the car crosses it at the moment of its BEST read — but the hardware
    line-crossing event fires ~instantly while the matching ANPR plate read is
    emitted 1-3s LATER (plate recognition lag). So we must NOT flush on the
    crossing (only the early/wrong reads are in by then). Instead the crossing
    just CONFIRMS the car physically entered; the background flusher then writes
    the entry once the burst settles (idle window), by which time the lagging
    correct read has arrived and wins. Net: the entry is sent after CAM-23, with
    the right plate.

    `allow_silent_entry` distinguishes the two confirm cameras:
      • CAM-23 (the ramp-top line) confirms the current burst; if it finds no
        buffered read at all, the ANPR truly missed the plate → hold a pending
        crossing that becomes a silent-entry alert if no burst arrives.
        (allow_silent_entry=True)
      • CAM-03 sits deep in the garage and fires LATER, usually after the burst
        already flushed — finding no open burst is normal, NOT a silent entry.
        Its image is then attached to the just-written entry instead.
        (allow_silent_entry=False)

    Both confirming cameras' snapshots are kept (one per source) and each is
    forwarded to the PMS under its own direction marker — so the PMS receives a
    ramp-top (CAM-23) AND an in-garage (CAM-03) image per car.
    """
    to_forward = None
    async with _bursts_lock:
        now = facility_now_naive()
        max_age = settings.ANPR_BURST_MAX_SECONDS
        # Only an OPEN, still-active burst may be confirmed by this crossing:
        #  • skip `force_flush` bursts — those are a previous car already closed
        #    for the flusher; their own crossing fired earlier, so this crossing
        #    belongs to the next car, not them.
        #  • skip stale bursts (older than the hard cap) — leftover false ANPR
        #    triggers must not be revived into a real entry by an unrelated car.
        open_bursts = [
            b for b in _entry_bursts.values()
            if b["reads"] and not b["force_flush"]
            and (now - b["first_event_time"]).total_seconds() <= max_age
        ]
        if open_bursts:
            buf = max(open_bursts, key=lambda b: b["last_read_at"])
            buf["confirmed"] = True
            if snapshot:
                buf["confirm_snapshots"].setdefault(source_cam, snapshot)
            if not buf["confirm_source"]:
                buf["confirm_source"] = source_cam
            logger.info(
                f"[UC1] Entry crossing confirmed by {source_cam} → burst "
                f"id={buf['id']} (reads={len(buf['reads'])}); waiting for the "
                "ANPR burst to settle before flushing"
            )
        elif allow_silent_entry:
            _pending_crossings.append({
                "ts": facility_now_naive(),
                "snapshot": snapshot,
                "source": source_cam,
            })
            logger.info(
                f"[UC1] Ramp crossing from {source_cam} with no buffered ANPR "
                "read — held as pending (burst may still arrive)"
            )
        else:
            # No open burst — the entry was likely already flushed (the normal
            # CAM-03 ordering). Attach this image to that just-written entry.
            to_forward = _claim_recent_entry_image(source_cam, snapshot, now)
            if to_forward is None:
                logger.debug(
                    f"[UC1] {source_cam} crossing with no open burst and no "
                    "recent entry to attach to — no action"
                )

    if to_forward is not None:
        await _forward_confirm_snapshot(*to_forward)


def _claim_recent_entry_image(source_cam: str, snapshot: str | None, now) -> tuple | None:
    """Match a late confirmation crossing to a recently flushed entry so its image
    can be forwarded with that entry's plate. Must be called while holding
    `_bursts_lock`. Prunes stale entries, picks the most-recent entry that has not
    yet received this source's image, marks it sent, and returns
    `(plate, source_cam, snapshot)` — or None if there's nothing to attach.

    KNOWN LIMITATION (tailgating): matching is recency-only — the crossing carries
    no plate/lane to correlate it with a specific car. Two cars entering within
    ENTRY_CONFIRM_MATCH_SECONDS can have their CAM-03 images swapped (A's garage
    image attaches to B). Acceptable for spaced entries; the per-source
    `sent_sources` guard still prevents the same image being sent to two cars."""
    if not snapshot:
        return None
    window = settings.ENTRY_CONFIRM_MATCH_SECONDS
    _recent_entries[:] = [
        e for e in _recent_entries if (now - e["ts"]).total_seconds() <= window
    ]
    for e in reversed(_recent_entries):   # most recent first
        if source_cam not in e["sent_sources"]:
            e["sent_sources"].add(source_cam)
            return (e["plate"], source_cam, snapshot)
    return None


async def confirm_pending_entry(db: Session, cam03_snapshot: str | None = None) -> None:
    """Backward-compatible entry point used by occupancy_service when CAM-03
    fires in the entry direction. CAM-03 is a deep/secondary confirmation, so it
    never raises a silent-entry alert."""
    await confirm_entry_crossing(
        db, snapshot=cam03_snapshot, source_cam="CAM-03", allow_silent_entry=False,
    )


async def flush_due_entry_bursts(db: Session) -> None:
    """Periodic background flush (own DB session). Writes confirmed/closed bursts
    labeled by their winning (last) read, drops never-confirmed ghosts, and
    raises silent-entry alerts for ramp crossings that never matched a burst.
    Commits once if anything changed."""
    now = facility_now_naive()
    require_confirm = settings.USE_CAM03_ENTRY_CONFIRMATION
    window = settings.ANPR_BURST_WINDOW_SECONDS
    max_age = settings.ANPR_BURST_MAX_SECONDS

    to_write: list[dict] = []
    to_drop: list[dict] = []
    silent: list[dict] = []
    async with _bursts_lock:
        for bid, buf in list(_entry_bursts.items()):
            age = (now - buf["first_event_time"]).total_seconds()
            idle = (now - buf["last_read_at"]).total_seconds()
            eligible = buf["force_flush"] or idle > window or age > max_age
            if not eligible:
                continue
            if require_confirm and not buf["confirmed"]:
                # Hold for a ramp crossing; if none arrives by the hard cap the
                # burst was a false ANPR trigger (no car) → drop it.
                if age > max_age:
                    to_drop.append(_entry_bursts.pop(bid))
                continue
            to_write.append(_entry_bursts.pop(bid))

        keep = []
        for c in _pending_crossings:
            if (now - c["ts"]).total_seconds() > window:
                silent.append(c)
            else:
                keep.append(c)
        _pending_crossings[:] = keep

    changed = False
    for buf in to_write:
        await _flush_entry_burst(db, buf)
        changed = True
    for buf in to_drop:
        logger.info(
            f"[UC1] Entry burst dropped (no ramp confirmation within {max_age:.0f}s): "
            f"cam={buf['camera_id']} reads={len(buf['reads'])}"
        )
    for c in silent:
        await _raise_silent_entry_alert(db, c.get("source"), c.get("snapshot"))
        changed = True
    if changed:
        db.commit()


async def _flush_entry_burst(db: Session, buf: dict) -> None:
    """Write ONE entry for a burst, labeled by the LAST read (winning plate).
    Runs dedup, anti-bounce, the PMS-API forward (port 8000) and vehicle
    resolution here — once, on the winning plate, so the wrong early read never
    reaches the DB, the vehicles table, or the PMS."""
    reads = buf["reads"]
    if not reads:
        return

    # Winning read = last of the burst: highest picNum, then latest arrival.
    winner = max(
        reads,
        key=lambda r: (r["pic_num"] if r["pic_num"] is not None else -1, r["event_time"]),
    )
    plate = winner["plate"]
    event_time = winner["event_time"]
    snapshot_path = winner["snapshot_path"]
    local_snapshot = winner["local_snapshot_path"]
    camera_id = buf["camera_id"]
    confidence = winner["confidence"]
    discarded = sorted({r["plate"] for r in reads if r["plate"] != plate})
    logger.info(
        f"[UC1] Flushing entry burst cam={camera_id} winner={plate} "
        f"pic={winner['pic_num']} conf={confidence} reads={len(reads)} "
        f"discarded={discarded}"
    )

    # Deduplication: same plate already logged as an entry in the last 30s.
    dedup_window = event_time - timedelta(seconds=30)
    recent = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == "entry",
            EntryExitLog.event_time >= dedup_window,
        )
        .first()
    )
    if recent:
        logger.debug(f"[UC1] Duplicate entry suppressed for plate={plate}")
        return

    # Anti-bounce: an entry whose plate just exited within the window is the
    # entry camera catching a car driving away from the exit gate — suppress.
    antibounce_s = settings.ENTRY_ANTIBOUNCE_SECONDS
    if antibounce_s > 0:
        recent_exit_window = event_time - timedelta(seconds=antibounce_s)
        recent_exit = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == plate,
                EntryExitLog.gate == "exit",
                EntryExitLog.event_time >= recent_exit_window,
            )
            .order_by(EntryExitLog.event_time.desc())
            .first()
        )
        if recent_exit:
            gap_s = (event_time - recent_exit.event_time).total_seconds()
            logger.info(
                "[UC1] Anti-bounce: suppressed entry for plate=%s "
                "(last exit %.1fs ago, window=%ds)",
                plate, gap_s, antibounce_s,
            )
            return

    # Register this entry in the recent-entries cache BEFORE any network I/O, so a
    # CAM-03 crossing that fires during the forwards below (the normal ordering)
    # can already attach its image. We're past dedup/anti-bounce here, so the entry
    # is committed-to. sent_sources seeds with the snapshots collected so far, so a
    # CAM-03 already in confirm_snapshots is not double-sent, while a CAM-03 that
    # arrives mid-flush finds this entry instead of falling through to an older car.
    confirm_snapshots = buf.get("confirm_snapshots") or {}
    async with _bursts_lock:
        match_window = settings.ENTRY_CONFIRM_MATCH_SECONDS
        prune_before = facility_now_naive()
        _recent_entries[:] = [
            e for e in _recent_entries
            if (prune_before - e["ts"]).total_seconds() <= match_window
        ]
        _recent_entries.append({
            "plate": plate,
            "ts": facility_now_naive(),
            "sent_sources": set(confirm_snapshots.keys()),
        })

    # Forward the WINNING plate + its snapshot to the PMS tracking API (port
    # 8000) — once, with the correct plate (never the wrong early read).
    try:
        await core_backend_client.notify_pms_anpr(
            plate, "entry", image_path=local_snapshot or snapshot_path,
        )
    except Exception as e:
        logger.warning(f"[UC1] PMS API forwarding failed for plate={plate}: {e}")

    # UC4: resolve the vehicle for the WINNING plate only — so the only vehicles
    # row this entry creates is for the final, correct plate.
    vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"

    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate="entry",
        camera_id=camera_id,
        event_time=event_time,
        snapshot_path=snapshot_path,
        plate_confidence=confidence,
        created_at=facility_now_naive(),
    )
    db.add(log_entry)
    parking_session_service.open_session(
        db,
        plate_number=plate,
        event_time=event_time,
        camera_id=camera_id,
        snapshot_path=snapshot_path,
        vehicle=vehicle,
    )
    logger.info(
        f"[UC1] Entry CONFIRMED (burst flush): plate={plate} "
        f"source={buf.get('confirm_source') or 'idle/boundary'}"
    )

    # Forward every confirmation snapshot collected so far (CAM-23, and CAM-03 if
    # it already fired) — each under its own PMS direction marker, after the gate
    # image. (This entry was already recorded in _recent_entries above, before the
    # network I/O, so a CAM-03 crossing arriving LATER can attach its image to it.)
    for src, image in confirm_snapshots.items():
        if image:
            await _forward_confirm_snapshot(plate, src, image)

    # UC4 alert/notification.
    if vehicle and vehicle.is_registered:
        from app.services.alert_service import broadcast_event
        await broadcast_event(
            is_alert=False,
            severity="info",
            event_type="AccessControllerEvent",
            description=f"Registered vehicle at entry gate: plate {plate}",
            camera_id=camera_id,
            zone_id="entry",
            plate_number=plate,
            snapshot_path=snapshot_path,
            triggered_at=event_time,
        )
    else:
        logger.info(f"[UC4] Triggering alert for unknown/unregistered vehicle: {plate}")
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=camera_id,
            zone_id="entry",
            event_type="AccessControllerEvent",
            description=f"Unregistered vehicle at entry gate: plate {plate}",
            plate_number=plate,
            snapshot_path=snapshot_path,
        )


async def _raise_silent_entry_alert(db: Session, source_cam: str | None,
                                    snapshot: str | None) -> None:
    """A car physically crossed the entry ramp but the ANPR read no plate."""
    logger.warning(
        f"[UC1] SILENT ENTRY: ramp crossing from {source_cam} with no ANPR plate read"
    )
    await create_alert(
        db=db,
        alert_type="silent_entry",
        camera_id=source_cam or "CAM-23",
        zone_id="entry",
        event_type="linedetection",
        description="Vehicle crossed entry ramp with no plate read (ANPR miss)",
        plate_number=None,
        snapshot_path=snapshot,
    )


async def _forward_confirm_snapshot(plate: str, source_cam: str,
                                    snapshot: str | None) -> None:
    """Fire-and-forget push of a confirmation image to the PMS API, under the
    direction marker mapped from its source camera. Failures are logged, never
    raised — same contract as the ANPR forwarding."""
    if not snapshot:
        return
    direction = _confirm_direction(source_cam)
    try:
        await core_backend_client.notify_pms_anpr(
            plate, direction, image_path=snapshot,
        )
    except Exception as e:
        logger.warning(
            f"[UC1] PMS confirmation snapshot ({source_cam}/{direction}) "
            f"forwarding failed for plate={plate}: {e}"
        )


async def handle_anpr_event(event: ParsedCameraEvent, db: Session):
    """
    Process ANPR events to log vehicle movement, identify owners,
    and calculate parking duration for exits.

    Entry reads are buffered (multi-read burst → write the last/correct plate
    once, at flush). Exit reads are written immediately (a single exit read is
    reliable; the user only reports entry mislabeling).
    """
    plate = event.plate_number

    camera_config = settings.CAMERAS.get(event.camera_id, {})
    gate = camera_config.get("gate", event.gate)

    if not gate:
        logger.warning(f"[Phase2] Dropped ANPR event from {event.camera_id} - no gate assigned (IP or Serial mapping missing)")
        return

    if not plate:
        logger.debug(f"[Phase2] ANPR event with no plate from {event.camera_id} - skipped")
        return

    # Camera sends tz-aware timestamps like `2026-05-07T12:14:51+03:00`. The
    # DB convention (since 2026-05-07) is NAIVE FACILITY-LOCAL — the wall
    # clock the operator sees, NOT UTC. Convert to facility tz first, then
    # strip tzinfo, so 12:14:51+03:00 stays 12:14:51 in the column.
    event_time = event.trigger_time or facility_now_naive()
    if event_time.tzinfo is not None:
        event_time = event_time.astimezone(facility_tz()).replace(tzinfo=None)

    # ── ENTRY: buffer the multi-read burst — the LAST read wins. The DB write,
    # PMS forward (port 8000) and vehicle resolution all happen once, at flush
    # time, on the winning plate. Nothing is written to the DB here. ────────
    if gate == "entry":
        await _buffer_entry_read(event, plate, event_time)
        return

    # ── EXIT: a single read is reliable — log immediately. ──────────────────
    logger.debug(f"[UC1] Checking dedup for plate {plate}...")
    dedup_window = event_time - timedelta(seconds=30)
    recent = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == gate,
            EntryExitLog.event_time >= dedup_window,
        )
        .first()
    )
    if recent:
        logger.debug(f"[UC1] Duplicate suppressed for plate={plate} gate={gate}")
        return

    # Forward plate + snapshot to PMS tracking API (fire-and-forget)
    try:
        await core_backend_client.notify_pms_anpr(
            plate, gate, image_path=event.local_snapshot_path or event.snapshot_path,
        )
    except Exception as e:
        logger.warning(f"[UC1] PMS API forwarding failed for plate={plate}: {e}")

    # UC4: Resolve vehicle identity via vehicle_service
    logger.debug(f"[UC4] Looking up vehicle for plate {plate}...")
    vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type}")

    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate=gate,
        camera_id=event.camera_id,
        event_time=event_time,
        snapshot_path=event.snapshot_path,
        created_at=facility_now_naive(),
    )

    # UC2: Calculation of Parking Duration on Exit
    matching_entry = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == "entry",
            EntryExitLog.matched_entry_id.is_(None)
        )
        .order_by(EntryExitLog.event_time.desc())
        .first()
    )

    if matching_entry:
        t2 = matching_entry.event_time
        if t2.tzinfo is not None:
            t2 = t2.astimezone(facility_tz()).replace(tzinfo=None)

        duration_seconds = int((event_time - t2).total_seconds())
        log_entry.parking_duration = max(0, duration_seconds)

        db.add(log_entry)
        db.flush()

        log_entry.matched_entry_id = matching_entry.id
        matching_entry.matched_entry_id = log_entry.id

        mins, secs = divmod(duration_seconds, 60)
        logger.info(f"[UC2] MATCH FOUND! Vehicle {plate} parked for {mins}m {secs}s")
    else:
        logger.warning(f"[UC2] No matching entry found for vehicle {plate}")

    parking_session_service.close_session(
        db,
        plate_number=plate,
        event_time=event_time,
        camera_id=event.camera_id,
        snapshot_path=event.snapshot_path,
    )

    # UC4
    if vehicle and vehicle.is_registered:
        from app.services.alert_service import broadcast_event
        await broadcast_event(
            is_alert=False,
            severity="info",
            event_type="AccessControllerEvent",
            description=f"Registered vehicle at exit gate: plate {plate}",
            camera_id=event.camera_id,
            zone_id=gate,
            plate_number=plate,
            snapshot_path=event.snapshot_path,
            triggered_at=event_time
        )
    else:
        logger.info(f"[UC4] Triggering alert for unknown/unregistered vehicle: {plate}")
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type="AccessControllerEvent",
            description=f"Unregistered vehicle at exit gate: plate {plate}",
            plate_number=plate,
            snapshot_path=event.snapshot_path,
        )

    if log_entry not in db.new:
        db.add(log_entry)
