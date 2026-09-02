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
from dataclasses import dataclass
from datetime import datetime, timedelta
from time import monotonic
from typing import Optional

from sqlalchemy.orm import Session

from app.services.entry_state_lock import acquire_plate_transaction_lock
from app.models.entry_exit_log import EntryExitLog
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.services import exit_pipeline
from app.services import parking_session_service
from app.services import plate_correction_service
from app.services import vehicle_service
from app.services.event_parser import (
    ParsedCameraEvent,
    normalize_plate,
    same_vehicle_plate,
)
from app.services.alert_service import create_alert
from app.services import hikcentral
from app.services.hikcentral import HikImages
from app.config import settings, facility_now_naive, facility_tz
from app.utils.logger import get_logger
from app.utils import core_backend_client

logger = get_logger(__name__)


class SourceTimestampUnavailable(RuntimeError):
    """A destructive exit cannot be ordered from a PMS receive-time fallback."""

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

# Strong refs to in-flight detached PMS forwards (see _spawn_confirm_forward).
_background_forwards: set[asyncio.Task] = set()


@dataclass(frozen=True)
class AnprPostCommitForward:
    """Legacy VA notification that must run only after the DB lock is released."""

    plate: str
    direction: str
    image_path: Optional[str]
    captured_at: datetime

    async def deliver(self) -> None:
        try:
            await core_backend_client.notify_pms_anpr(
                self.plate,
                self.direction,
                image_path=self.image_path,
                captured_at=self.captured_at,
            )
        except Exception as exc:
            logger.warning(
                "[UC1] PMS API forwarding failed for plate=%s: %s",
                self.plate,
                exc,
            )
            if (
                settings.ENTRY_V2_MODE == "authoritative"
                and self.direction.strip().lower() == "exit"
            ):
                raise


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
    now = monotonic()
    valid_index = next(
        (
            index
            for index, crossing in enumerate(_pending_crossings)
            if now <= crossing["expires_at_monotonic"]
        ),
        None,
    )
    if valid_index is not None:
        c = _pending_crossings.pop(valid_index)
        buf["confirmed"] = True
        src = c.get("source") or "CAM-23"
        if c.get("snapshot"):
            buf["confirm_snapshots"].setdefault(src, c["snapshot"])
        buf["confirm_source"] = src


def _is_same_car_reread(buf: dict, plate: str, event_time) -> bool:
    """True when a picNum tie is one car re-read, not the next car arriving.

    `pic <= max_pic` is genuinely ambiguous — a new car whose pic 1-2 events were
    lost also opens at 3 — so the plate has to carry the decision. A read whose
    plate is a truncation of one already buffered (or identical to it), arriving
    within seconds, is the same car: two cars cannot occupy one gate that close
    together, and their plates would have to be truncations of each other on top
    of that. Anything else still splits, exactly as before.
    """
    limit = settings.ANPR_BURST_SAME_CAR_SECONDS
    for read in buf["reads"]:
        if not same_vehicle_plate(read["plate"], plate):
            continue
        previous = read["event_time"]
        if event_time is None or previous is None:
            return True     # no usable timestamps — the plate match is the evidence
        if abs((event_time - previous).total_seconds()) <= limit:
            return True
    return False


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
            if picnum_reset and _is_same_car_reread(buf, plate, event_time):
                # Not a new car: the SAME plate came back truncated under a
                # repeated picNum. Splitting here is what wrote KKR-4 for
                # KKR-6294 on 2026-08-09 — the good read was force-flushed into
                # a burst the ramp crossing could no longer confirm, so it was
                # dropped as a ghost while the partial read became the entry.
                logger.info(
                    f"[UC1] Same-car re-read kept in burst id={buf['id']}: "
                    f"plate={plate} pic={pic} (already buffered up to pic={max_pic})"
                )
                picnum_reset = False
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
                "expires_at_monotonic": (
                    monotonic() + settings.ENTRY_PENDING_CROSSING_SECONDS
                ),
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
        _spawn_confirm_forward(*to_forward)


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
    now_monotonic = monotonic()
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
            if now_monotonic > c["expires_at_monotonic"]:
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
        # The refusal only sticks if HikCentral's record for the same pass is
        # consumed too — otherwise the reconciler re-opens it. See
        # _tombstone_refused_burst.
        if await _tombstone_refused_burst(db, buf):
            changed = True
    for c in silent:
        # A crossing with no plate is only "silent" once HikCentral has also
        # failed to name the car. Recovery is attempted first, never after.
        if await _recover_silent_entry(db, c):
            changed = True
            continue
        await _raise_silent_entry_alert(db, c.get("source"), c.get("snapshot"))
        changed = True
    if changed:
        db.commit()


def _winning_read(reads: list[dict]) -> dict:
    """The read that labels a burst: highest picNum, then confidence, then latest.

    The entry ANPR fires several reads per car and the early ones are the bad
    ones (plate far/small/blurry), so the burst is labeled by its last read. Kept
    as one function because the refusal tombstone must name the same plate the
    flush would have written — if these two ever disagreed, a refused burst would
    tombstone one plate and the reconciler would re-open under another.

    picNum stays the primary key: a later frame really is the better one. But the
    camera repeats a picNum often enough that arrival order alone decided those
    ties, which on 2026-08-09 labeled a car KKR-4 (conf 89) when the same burst
    held KKR-6294 (conf 96) at the same picNum. Confidence breaks the tie before
    arrival order does; `event_time` still settles the rest.
    """
    return max(
        reads,
        key=lambda r: (
            r["pic_num"] if r["pic_num"] is not None else -1,
            r["confidence"] if r["confidence"] is not None else -1,
            r["event_time"],
        ),
    )


# Refusals the gate made but could not tombstone. Each entry blocks the
# reconciler from re-opening that pass, permanently.
#
# THE RULE: an ANPR read with no ramp crossing is not a car that entered. Every
# burst in here was dropped for exactly that reason — the plate was read at the
# gate and no CAM-23/CAM-03 crossing followed within ANPR_BURST_MAX_SECONDS — so
# there is no entry to recover and nothing the sweep should ever open. The block
# does not expire, because the fact it encodes does not expire.
#
# Measured 2026-09-02 PM: six gate reads, ZERO ramp crossings in six hours. All
# six were passing traffic (confidences 62-96 against the morning's uniform 96);
# one was a car the entry camera read nine seconds AFTER it exited. The single
# one that got opened, USB-6662, became an imageless session nothing can close.
#
# The cost of being wrong here is deliberately asymmetric. Blocking a car that
# really did enter costs an unmatched exit, which announces itself and closes
# itself — ERS-7949 and EED-7286 did exactly that on 2026-09-02. Opening one
# that did not costs a session no mechanism can ever close.
#
# HIK_REFUSAL_HOLD_SECONDS bounds how long we keep RETRYING the tombstone, not
# how long the block lasts. A tombstone is the durable form of the same refusal;
# failing to write one never weakens it.
#
# Deliberately in RAM and bounded. The durable alternative is a HikValidation
# row, and that is not available here: `_catchup_watermark` reads MAX(pass_time)
# across every row for a direction, so a synthetic tombstone would advance the
# watermark past records nobody has processed and lose them silently. A restart
# drops these and the reconciler may re-open one pass — the same behaviour as
# before this guard existed, so the failure mode is unchanged, never worsened.
#
# (plate, event_time, retry_until)
_UNVERIFIED_REFUSALS: list[tuple[str, datetime, datetime]] = []
_UNVERIFIED_REFUSALS_MAX = 256


def _remember_unverified_refusal(
    plate: str, event_time: datetime, why: str = "HikCentral did not answer"
) -> None:
    # Store canonical, because the reconciler compares against
    # `rec.canonical_plate`. The burst carries the camera's raw read.
    plate = normalize_plate(plate) or plate
    retry_until = facility_now_naive() + timedelta(
        seconds=settings.HIK_REFUSAL_HOLD_SECONDS
    )
    logger.warning(
        "[UC1] Refusal for plate=%s at %s could not be tombstoned (%s) — the "
        "reconciler is blocked from this pass for good; retrying the tombstone "
        "until %s",
        plate, event_time, why, retry_until,
    )
    _UNVERIFIED_REFUSALS.append((plate, event_time, retry_until))
    if len(_UNVERIFIED_REFUSALS) > _UNVERIFIED_REFUSALS_MAX:
        del _UNVERIFIED_REFUSALS[:-_UNVERIFIED_REFUSALS_MAX]


def _matches_unverified_refusal(plate: str, when: datetime, window: timedelta) -> bool:
    """True when the gate refused this pass but could not prove it to HikCentral.

    Plate comparison is `same_vehicle_plate`, not equality: the refusal is
    recorded under the burst's winning read, and the HikCentral record may carry
    a truncation of the same plate.
    """
    for refused_plate, refused_at, _hold_until in _UNVERIFIED_REFUSALS:
        if abs((refused_at - when).total_seconds()) > window.total_seconds():
            continue
        if same_vehicle_plate(refused_plate, plate):
            return True
    return False


async def _retry_unverified_refusals(db: Session) -> None:
    """Try again to tombstone refusals we could not prove the first time.

    Runs at the head of the reconcile sweep, so every attempt to re-open a
    refused pass is preceded by one more chance to make the refusal durable.

    Nothing here releases a pass. An entry leaves this list in exactly one way:
    the tombstone is finally written, which is the same refusal made durable.
    Past `retry_until` we simply stop spending HikCentral calls on it and let
    the in-memory block stand.

    Sizing the retry window by "how long until the pass ages out of the sweep's
    reach" would not work even if we wanted it to: there is no such point.
    `_reconcile_window` anchors on the watermark, so a stale one re-walks
    arbitrarily far back — USB-6662 on 2026-09-02 was opened 3h27m after its
    pass, off a 3h59m watermark gap, when the block was still time-bounded.
    """
    if not _UNVERIFIED_REFUSALS:
        return
    now = facility_now_naive()
    still_blocked: list[tuple[str, datetime, datetime]] = []
    for plate, event_time, retry_until in list(_UNVERIFIED_REFUSALS):
        if now >= retry_until:
            # Stop asking, keep blocking.
            still_blocked.append((plate, event_time, retry_until))
            continue
        try:
            guid = await hikcentral.consume_refused_entry(
                db, plate, event_time, require_plate_match=True
            )
        except Exception:
            still_blocked.append((plate, event_time, retry_until))
            continue
        if guid is None:
            # Still nothing to consume. The record may simply not have reached
            # us yet, and that is the case this guard exists for.
            still_blocked.append((plate, event_time, retry_until))
            continue
        # consume_refused_entry only stages the row; the caller commits. Without
        # this the GUID is still unconsumed when list_unconsumed_records runs
        # immediately below, and the pass is re-opened anyway.
        db.commit()
        logger.info(
            "[UC1] Refusal for plate=%s at %s tombstoned on retry (guid=%s) — "
            "the block is now durable",
            plate, event_time, guid,
        )
    _UNVERIFIED_REFUSALS[:] = still_blocked


async def _tombstone_refused_burst(db: Session, buf: dict) -> bool:
    """Stop the HikCentral reconciler from re-opening a burst the gate refused.

    A dropped burst leaves no `EntryExitLog` on purpose. `_reconcile_missed
    _entries` treats "HikCentral has a pass with no EntryExitLog" as a missed
    entry, so the refusal would be silently undone a few minutes later — which is
    exactly what produced the phantom open sessions that surface as overstays.
    Consuming the GUID here closes that loop. Returns True when a row was added.
    """
    reads = buf.get("reads") or []
    if not reads:
        return False
    winner = _winning_read(reads)
    plate = winner.get("plate")
    event_time = winner.get("event_time") or buf.get("first_event_time")
    if not plate or event_time is None:
        return False
    try:
        guid = await hikcentral.consume_refused_entry(db, plate, event_time)
    except hikcentral.RefusalLookupFailed:
        # We could not ask HikCentral, so we do NOT know whether a record for
        # this pass exists. Failing open here is what re-opened SUZ-975. Hold
        # the reconciler off this pass until a later sweep can tombstone it
        # properly.
        _remember_unverified_refusal(plate, event_time)
        return False
    except Exception as exc:  # never let a tombstone break the flusher
        logger.warning(
            "[UC1] Could not tombstone refused burst plate=%s: %r", plate, exc
        )
        _remember_unverified_refusal(plate, event_time)
        return False
    if guid is None:
        # We asked and HikCentral returned nothing unconsumed for this pass.
        # That reads like "there is nothing for the reconciler to re-open", and
        # it was treated that way — but crossRecords lags, and the record can
        # surface minutes later carrying a pass_time from BEFORE this refusal.
        # The sweep then opens the very pass the gate just refused. Hold it.
        _remember_unverified_refusal(
            plate, event_time, why="HikCentral returned no record yet"
        )
        return False
    return True


def _recovered_burst(outcome, crossing: dict) -> dict:
    """Shape a HikCentral recovery like an ordinary ANPR burst.

    Reusing the normal flush path is the point: dedup, anti-bounce, vehicle
    resolution, session creation, alerting and the VA forward all behave
    identically, so a recovered entry is indistinguishable downstream from one
    the camera labelled itself. `hik_outcome` rides along so the flush reuses the
    record already fetched instead of looking the same car up twice.
    """
    event_time = outcome.pass_time_local or crossing.get("ts")
    crossing_snapshot = crossing.get("snapshot")
    source_cam = crossing.get("source") or "CAM-23"
    return {
        "id": 0,
        # HikCentral saw this car at the entry LPR resource, so the gate camera
        # is the honest attribution — the ramp cam only proved it moved.
        "camera_id": "CAM-ENTRY",
        "reads": [{
            "plate": outcome.plate,
            "confidence": None,
            "pic_num": None,
            "event_time": event_time,
            "snapshot_path": None,
            "local_snapshot_path": None,
        }],
        "first_event_time": event_time,
        "last_read_at": facility_now_naive(),
        "confirmed": True,
        "confirm_snapshots": (
            {source_cam: crossing_snapshot} if crossing_snapshot else {}
        ),
        "confirm_source": source_cam,
        "force_flush": True,
        "hik_outcome": outcome,
    }


async def _recover_silent_entry(db: Session, crossing: dict) -> bool:
    """Try to rescue a plateless ramp crossing using HikCentral (Case B).

    Returns True when the crossing became a real entry. False restores the
    previous behaviour exactly: the caller raises the silent-entry alert.

    Recovery needs exactly one HikCentral candidate in the window — with two
    cars in flight nothing says which one crossed, and guessing would staple a
    stranger's plate onto the session.
    """
    outcome = await hikcentral.recover_entry_plate(
        crossing.get("ts"),
        crossing.get("source") or "CAM-23",
        db,
    )
    if outcome is None or not outcome.plate:
        return False

    logger.warning(
        "[UC1] Silent entry RECOVERED from HikCentral: plate=%s guid=%s "
        "source=%s",
        outcome.plate, outcome.guid, crossing.get("source"),
    )
    await _flush_entry_burst(db, _recovered_burst(outcome, crossing))
    return True


# ── HikCentral reconciliation (event-driven) ──────────────────────────────
# A gate-area event (CAM-23/03/ENTRY for entries, CAM-08/EXIT for exits) is the
# heartbeat that sweeps HikCentral for cars the edge pipeline missed ENTIRELY —
# neither the ANPR read nor the ramp/occupancy crossing reached PMS-AI, so no
# session exists at all. A missed entry opens a session; a missed exit closes
# one. Fire-and-forget and debounced per direction, so it never blocks the
# camera webhook and a busy camera cannot fire a HikCentral call per frame. The
# grace window keeps a sweep from racing a car still in the live pipeline, and
# the hik_validations GUID makes overlapping sweeps idempotent.
#
# NOTE: a reconciled session has NO physical ramp confirmation (that is the
# point — the edge missed it). HikCentral's server-side record is its only
# evidence, recorded with plate_source="hik_polled" for audit.

_last_reconcile_at: dict[str, float] = {}


def note_gate_event(camera_id: str) -> None:
    """Trigger a debounced HikCentral reconcile sweep for a gate-area event.

    Cheap and synchronous: decides the direction, applies the debounce, and (if
    due) spawns a background task. Safe to call from the camera webhook — it
    never awaits, never raises, and never delays the response.
    """
    if not hikcentral.is_enabled():
        return
    if camera_id in settings.hik_reconcile_entry_trigger_cameras():
        direction = hikcentral.DIRECTION_ENTRY
    elif camera_id in settings.hik_reconcile_exit_trigger_cameras():
        direction = hikcentral.DIRECTION_EXIT
    else:
        return

    now = monotonic()
    if now - _last_reconcile_at.get(direction, 0.0) < settings.HIK_RECONCILE_DEBOUNCE_SECONDS:
        return
    _last_reconcile_at[direction] = now

    try:
        asyncio.get_running_loop().create_task(_run_reconcile(direction))
    except RuntimeError:
        # No running loop (sync context/tests): skip rather than raise.
        pass


_sweep_in_flight: set[str] = set()


async def _run_reconcile(direction: str) -> None:
    """Background sweep with its own DB session. Never raises.

    Walks from the last HikCentral pass we actually consumed up to now, so every
    gate event heals whatever fell in the gap since the previous one. After a
    normal quiet period that span is minutes; after an outage it is however long
    the outage was, and the sweep closes all of it.
    """
    from app.database import SessionLocal

    # A long catch-up can outlive the 30s debounce. Two sweeps of the same
    # direction would re-query the same span and race each other on the same
    # records; GUID uniqueness keeps that correct but it is wasted platform
    # load, so the second one simply yields to the one already running.
    if direction in _sweep_in_flight:
        logger.info("[Hik][reconcile] %s sweep already running — skipping", direction)
        return
    _sweep_in_flight.add(direction)

    db = SessionLocal()
    try:
        sweep = (
            _reconcile_missed_entries
            if direction == hikcentral.DIRECTION_ENTRY
            else _reconcile_missed_exits
        )
        begin, end = _reconcile_window(db, direction)
        if begin >= end:
            return
        # query_vehicle_logs does NOT paginate (pageNo=1, newest-first), so a
        # window holding more than HIK_RECONCILE_PAGE_SIZE records silently
        # drops the OLDEST — exactly what a catch-up is hunting for. Walk it.
        chunk = timedelta(minutes=settings.HIK_CATCHUP_CHUNK_MINUTES)
        if end - begin > chunk:
            logger.warning(
                "[Hik][reconcile] %s: %s gap since the last consumed pass (%s) "
                "— sweeping it in %s chunks",
                direction, end - begin, begin, chunk,
            )
        cursor = begin
        while cursor < end:
            upper = min(cursor + chunk, end)
            try:
                await sweep(db, window=(cursor, upper))
            except Exception as exc:
                # One bad chunk must not abandon the rest of the gap.
                logger.warning(
                    "[Hik][reconcile] %s chunk %s..%s failed: %r",
                    direction, cursor, upper, exc,
                )
                db.rollback()
            cursor = upper
    except Exception as exc:  # a sweep must never escape into the event loop
        logger.warning("[Hik][reconcile] %s sweep failed: %r", direction, exc)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        _sweep_in_flight.discard(direction)
        db.close()


async def startup_catchup() -> None:
    """Run both sweeps once at boot, without waiting for a car to show up.

    The watermark window in `_reconcile_window` already recovers an outage on
    the first gate event after a restart — but only when one arrives. A service
    that comes back at 02:00 would otherwise sit on the gap until the morning
    rush, reporting overstays the whole time. Never raises: a HikCentral that is
    unreachable at boot must not stop the service from starting.
    """
    if not settings.HIK_CATCHUP_ON_STARTUP or not hikcentral.is_enabled():
        return
    for direction in (hikcentral.DIRECTION_EXIT, hikcentral.DIRECTION_ENTRY):
        await _run_reconcile(direction)


def _catchup_watermark(db: Session, direction: str) -> Optional[datetime]:
    """The newest HikCentral pass this deployment has already consumed.

    `hik_validations.pass_time` is the only durable record of how far the gate
    pipeline actually got, and the GUID uniqueness that backs it is what makes
    re-sweeping the same span harmless. Returns None when the table holds
    nothing for this direction — a fresh DB has no gap to close.
    """
    row = (
        db.query(HikValidation.pass_time)
        .filter(
            HikValidation.direction == direction,
            HikValidation.pass_time.isnot(None),
        )
        .order_by(HikValidation.pass_time.desc())
        .first()
    )
    return row[0] if row else None


def _reconcile_window(db: Session, direction: str) -> tuple[datetime, datetime]:
    """[begin, grace] window in naive facility-local time, anchored on the
    last HikCentral pass this deployment actually consumed.

    A fixed lookback cannot heal downtime. On 2026-08-09 PMS-AI stopped
    ingesting for ~4h; 25 exits landed in that hole, and when it came back the
    15-minute window could only see the last few minutes of it. Every one of
    those sessions stayed open and surfaced as a 24h+ overstay for cars that had
    driven home hours earlier.

    Anchoring on the watermark makes each sweep self-healing: it resumes exactly
    where the previous one stopped, so a gap of any length is picked up by the
    next gate event. HIK_RECONCILE_LOOKBACK_SECONDS remains the FLOOR (never ask
    for less than that, so a fresh or stale watermark still gets normal cover)
    and HIK_CATCHUP_MAX_HOURS the ceiling, so a DB restored from an old backup
    cannot trigger a week-long sweep.
    """
    now = facility_now_naive()
    end = now - timedelta(seconds=settings.HIK_RECONCILE_GRACE_SECONDS)
    default_begin = now - timedelta(seconds=settings.HIK_RECONCILE_LOOKBACK_SECONDS)
    mark = _catchup_watermark(db, direction)
    if mark is None:
        return (default_begin, end)
    floor = now - timedelta(hours=settings.HIK_CATCHUP_MAX_HOURS)
    if mark < floor:
        logger.warning(
            "[Hik][reconcile] %s: last consumed pass %s predates the %sh cap — "
            "sweeping back only to %s. Older gaps need "
            "scripts/setup/backfill_missed_exits.py.",
            direction, mark, settings.HIK_CATCHUP_MAX_HOURS, floor,
        )
    return (min(default_begin, max(mark, floor)), end)


def _gate_event_already_logged(
    db: Session, plate: str, gate: str, when: datetime, window: timedelta
) -> bool:
    """True when an EntryExitLog for this plate/gate sits within ±window — i.e.
    the edge pipeline already noticed this pass.

    A truncated read of the SAME car counts as already logged. The camera files
    both its full and its partial read with HikCentral, but the burst now merges
    them and writes ONE entry under the fuller plate — leaving the partial record
    unconsumed. Matching on exact plate alone, this sweep would read that
    leftover as a car nobody logged and open a phantom session for it, which is
    the overstay-generating failure the crossing gate exists to prevent.

    Deliberately bidirectional. Two cars whose plates truncate to each other
    passing inside the match window is vanishingly rare, and if it ever happens,
    missing one entry (the car is still caught as an unmatched exit) is cheaper
    than an open session that never closes.
    """
    logged_plates = (
        db.query(EntryExitLog.plate_number)
        .filter(
            EntryExitLog.gate == gate,
            EntryExitLog.event_time >= when - window,
            EntryExitLog.event_time <= when + window,
        )
        .all()
    )
    return any(same_vehicle_plate(plate, row[0]) for row in logged_plates)


async def _reconcile_missed_entries(
    db: Session, window: Optional[tuple[datetime, datetime]] = None
) -> None:
    begin, end = window or _reconcile_window(db, hikcentral.DIRECTION_ENTRY)
    # One more chance to make any unverified refusal durable BEFORE we consider
    # re-opening anything — a tombstone written now removes the record below.
    await _retry_unverified_refusals(db)
    records = await hikcentral.list_unconsumed_records(
        settings.hik_entry_resource_ids(), begin, end, db
    )
    match_s = timedelta(seconds=settings.HIK_RECONCILE_MATCH_SECONDS)
    # Oldest-first. HikCentral returns newest-first (orderType=1) and each
    # processed record consumes its GUID, which is what the watermark reads. If
    # a record mid-way through raises, consuming the NEWEST first would leave
    # the watermark ahead of older unprocessed records in the same chunk — no
    # later sweep looks behind the watermark, so they would be lost silently.
    consumed = 0
    for rec in sorted(records, key=lambda r: hikcentral.polled_outcome(r).pass_time_local):
        outcome = hikcentral.polled_outcome(rec)
        pass_local = outcome.pass_time_local
        if _gate_event_already_logged(db, rec.canonical_plate, "entry", pass_local, match_s):
            # Nothing to repair — but consume it, or the watermark never advances
            # on a healthy day and the next sweep re-walks this same span.
            if hikcentral.consume_already_logged(db, rec, hikcentral.DIRECTION_ENTRY):
                db.commit()
                consumed += 1
            continue
        if _matches_unverified_refusal(rec.canonical_plate, pass_local, match_s):
            # The gate refused this pass; we just could not prove it to
            # HikCentral. Opening it would undo a decision the crossing gate
            # made on real evidence, which is exactly the overstay-generating
            # failure the tombstone exists to prevent. Leave the GUID
            # unconsumed so a later sweep can still tombstone it properly.
            logger.warning(
                "[Hik][reconcile] SKIPPING plate=%s guid=%s at %s — the gate "
                "refused this pass and the tombstone is still unverified",
                rec.canonical_plate, rec.guid, pass_local,
            )
            continue
        if not hikcentral.is_authoritative():
            logger.warning(
                "[Hik][reconcile] shadow: MISSED entry plate=%s guid=%s at %s "
                "(would open a session)",
                rec.canonical_plate, rec.guid, pass_local,
            )
            continue
        if settings.HIK_RECONCILE_REQUIRE_IMAGE and not outcome.vehicle_image_url:
            # No image means no appearance evidence, and the plate is whatever
            # HikCentral read. Neither the plate path nor the Re-ID fallback can
            # ever close the resulting session, so opening it would create a
            # permanent overstay rather than recover an entry. Left unconsumed
            # on purpose: a later sweep may find the same pass with imagery.
            logger.warning(
                "[Hik][reconcile] NOT opening plate=%s guid=%s at %s — the "
                "record carries no vehicle image, so the session could never "
                "be closed. Review it by hand.",
                rec.canonical_plate, rec.guid, pass_local,
            )
            continue
        # Reuse the exact recovery flush: dedup, vehicle, session, image,
        # hik_validation (which consumes the GUID) and the VA forward.
        await _flush_entry_burst(
            db,
            _recovered_burst(
                outcome, {"ts": pass_local, "source": "HIK-RECON", "snapshot": None}
            ),
        )
        db.commit()  # release locks before the next record's network await
        logger.warning(
            "[Hik][reconcile] OPENED missed entry plate=%s guid=%s at %s",
            rec.canonical_plate, rec.guid, pass_local,
        )
    if consumed:
        logger.info(
            "[Hik][reconcile] entry: %d pass(es) already logged by the edge — "
            "consumed, watermark advanced to %s",
            consumed, _catchup_watermark(db, hikcentral.DIRECTION_ENTRY),
        )


async def _reconcile_missed_exits(
    db: Session, window: Optional[tuple[datetime, datetime]] = None
) -> None:
    """Close sessions for HikCentral exits the edge pipeline never saw.

    `window` overrides the rolling [lookback, grace] window. The live sweep
    always passes None; only the one-off backfill after an ingest outage names
    an explicit range, because HIK_RECONCILE_LOOKBACK_SECONDS is deliberately
    short and can never reach back across hours of downtime.
    """
    begin, end = window or _reconcile_window(db, hikcentral.DIRECTION_EXIT)
    records = await hikcentral.list_unconsumed_records(
        settings.hik_exit_resource_ids(), begin, end, db
    )
    match_s = timedelta(seconds=settings.HIK_RECONCILE_MATCH_SECONDS)
    # Oldest-first. HikCentral returns newest-first (orderType=1) and each
    # processed record consumes its GUID, which is what the watermark reads. If
    # a record mid-way through raises, consuming the NEWEST first would leave
    # the watermark ahead of older unprocessed records in the same chunk — no
    # later sweep looks behind the watermark, so they would be lost silently.
    consumed = 0
    for rec in sorted(records, key=lambda r: hikcentral.polled_outcome(r).pass_time_local):
        outcome = hikcentral.polled_outcome(rec)
        pass_local = outcome.pass_time_local
        if _gate_event_already_logged(db, rec.canonical_plate, "exit", pass_local, match_s):
            # Nothing to repair — but consume it, or the watermark never advances
            # on a healthy day and the next sweep re-walks this same span.
            if hikcentral.consume_already_logged(db, rec, hikcentral.DIRECTION_EXIT):
                db.commit()
                consumed += 1
            continue
        if not hikcentral.is_authoritative():
            logger.warning(
                "[Hik][reconcile] shadow: MISSED exit plate=%s guid=%s at %s "
                "(would close the open session)",
                rec.canonical_plate, rec.guid, pass_local,
            )
            continue
        images = await hikcentral.download_hik_images(outcome)
        # The same pipeline the edge exit runs. Until this, the sweep stopped at
        # `close_session`: an exit recovered here for a car whose ENTRY plate was
        # misread matched nothing, closed nothing, and consumed its GUID on the
        # way out, so no later sweep could retry it. That is SNA-226.
        vehicle = vehicle_service.ensure_unregistered_vehicle(db, rec.canonical_plate)
        result = await exit_pipeline.resolve(
            db,
            exit_pipeline.from_polled_outcome(outcome, images.vehicle_image_path),
            vehicle=vehicle,
            exit_image_path=images.vehicle_image_path,
        )
        session = result.session
        # Before writing an audit row, ask whether the edge already wrote one for
        # THIS pass under a different plate — the misread this record corrects.
        # `_gate_event_already_logged` above cannot see that: it ties truncations
        # together, not two genuinely different strings, so a corrected plate
        # looked like a car nobody logged and earned one car a second exit row.
        late_row = exit_pipeline.exit_row_for_late_pass(db, pass_local, match_s)
        if late_row is not None:
            exit_pipeline.adopt_late_plate(db, late_row, rec.canonical_plate)
            _pair_audit_rows(db, late_row, session)
        else:
            _write_reconciled_exit_log(
                db, rec.canonical_plate, pass_local, images.vehicle_image_path,
                session=session,
            )
        # Consume the GUID (unique) so a later sweep never redoes this exit.
        hikcentral.record_hik_validation(
            db,
            outcome=outcome,
            direction=hikcentral.DIRECTION_EXIT,
            images=images,
            session_id=session.id if session else None,
            entry_exit_log_id=late_row.id if late_row is not None else None,
        )
        db.commit()
        await plate_correction_service.notify_va(result.correction)
        logger.warning(
            "[Hik][reconcile] %s plate=%s guid=%s at %s",
            "CORRECTED the edge exit row" if late_row is not None
            else "CLOSED missed exit" if session
            else "logged exit (no open session)",
            rec.canonical_plate, rec.guid, pass_local,
        )
    if consumed:
        logger.info(
            "[Hik][reconcile] exit: %d pass(es) already logged by the edge — "
            "consumed, watermark advanced to %s",
            consumed, _catchup_watermark(db, hikcentral.DIRECTION_EXIT),
        )


def _pair_audit_rows(
    db: Session, exit_log: EntryExitLog, session: Optional[ParkingSession]
) -> None:
    """Link an exit row to the entry row of the stay it actually closed.

    No-op when nothing closed, or when the pairing already happened on the exact
    plate. Looks the entry row up through the session so a corrected plate finds
    it — by this point `apply_correction` has already moved both onto the real
    plate, so they agree.
    """
    if session is None or exit_log.matched_entry_id is not None:
        return
    entry_log = parking_session_service.entry_log_for(db, session)
    if entry_log is None or entry_log.id == exit_log.id:
        return
    if exit_log.id is None:
        # The edge path does not add its row until the very end, so flushing
        # alone would not give it an id — and an id is the whole point: the
        # pairing is two-way, and the entry row needs this row's key.
        db.add(exit_log)
        db.flush()
    exit_log.matched_entry_id = entry_log.id
    entry_log.matched_entry_id = exit_log.id
    if exit_log.parking_duration is None:
        exit_log.parking_duration = max(
            0, int((exit_log.event_time - session.entry_time).total_seconds())
        )


def _write_reconciled_exit_log(
    db: Session,
    plate: str,
    when: datetime,
    snapshot: Optional[str],
    session: Optional[ParkingSession] = None,
) -> None:
    """Write the audit exit row for a reconciled exit, matched to its open entry
    with a computed duration — the same shape the live exit path produces.

    When the pipeline resolved a stay, that stay decides the pairing: its plate
    may differ from this pass's, and the session is the only thing that knows
    which entry this exit actually ends. The plate-based query below is the
    fallback for a recovered exit that closed nothing.
    """
    vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle.vehicle_type if vehicle else "unknown",
        gate="exit",
        camera_id="CAM-EXIT",
        event_time=when,
        snapshot_path=snapshot,
        created_at=facility_now_naive(),
    )
    matching_entry = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == "entry",
            EntryExitLog.matched_entry_id.is_(None),
            EntryExitLog.event_time <= when,
        )
        .order_by(EntryExitLog.event_time.desc())
        .first()
    )
    db.add(log_entry)
    db.flush()
    if session is not None:
        _pair_audit_rows(db, log_entry, session)
        return
    if matching_entry is not None:
        entry_time = matching_entry.event_time
        if entry_time.tzinfo is not None:
            entry_time = entry_time.astimezone(facility_tz()).replace(tzinfo=None)
        log_entry.parking_duration = max(0, int((when - entry_time).total_seconds()))
        log_entry.matched_entry_id = matching_entry.id
        matching_entry.matched_entry_id = log_entry.id



def _reconcile_stale_stays_on_reentry(
    db: Session,
    plate: str,
    event_time: datetime,
) -> None:
    """Close stays proven obsolete by this confirmed inward crossing.

    The Entry V2 twin of this lives in
    ``entry_confirmation_service._reconcile_older_open_sessions``, but that path
    only executes under ``ENTRY_V2_MODE=authoritative`` — shadow rewrites every
    CONFIRMED decision to ABSTAINED and PMS returns before touching the DB. The
    legacy burst-flush path is what actually opens stays in production, so the
    reconciliation has to exist here too.

    Never raises: a stale stay that cannot be closed must not cost the caller
    its entry. The worst case is the status quo — one stay left open.
    """
    if not settings.ENTRY_REENTRY_RECONCILE_ENABLED:
        return

    # DB columns are naive facility-local (see parking_session_service._naive);
    # normalize the same way before comparing against session.entry_time.
    boundary = event_time
    if boundary.tzinfo is not None:
        boundary = boundary.astimezone(facility_tz()).replace(tzinfo=None)
    min_age = timedelta(
        seconds=float(settings.ENTRY_REENTRY_RECONCILE_MIN_AGE_SECONDS)
    )
    try:
        stale = [
            session
            for session in parking_session_service.get_open_sessions(db, plate)
            if session.entry_time < boundary - min_age
        ]
    except Exception:
        logger.exception(
            "[UC1] Could not read open stays for plate=%s — entry proceeds", plate
        )
        return

    for session in stale:
        try:
            parking_session_service.reconcile_open_session_for_reentry(
                db, session, boundary
            )
            logger.warning(
                "[UC1] Re-entry closed stale stay id=%s plate=%s open since %s "
                "(%s) — its exit was never read",
                session.id,
                plate,
                session.entry_time,
                boundary - session.entry_time,
            )
        except Exception:
            logger.exception(
                "[UC1] Could not reconcile stale stay id=%s plate=%s — leaving "
                "it open",
                session.id,
                plate,
            )
    if stale:
        db.flush()


async def _flush_entry_burst(db: Session, buf: dict) -> None:
    """Write ONE entry for a burst, labeled by the LAST read (winning plate).
    Runs dedup, anti-bounce, the PMS-API forward (port 8000) and vehicle
    resolution here — once, on the winning plate, so the wrong early read never
    reaches the DB, the vehicles table, or the PMS."""
    reads = buf["reads"]
    if not reads:
        return

    winner = _winning_read(reads)
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

    # ── HikCentral validation (Case A) ───────────────────────────────────
    # CAM-23 has already confirmed the crossing by the time a burst flushes, so
    # this is the one place a car is validated — exactly one lookup per
    # candidate. Deliberately ABOVE every DB access: it is network I/O, and the
    # deadlock documented further down is what happens when an await separates a
    # write from its commit.
    #
    # A burst recovered from HikCentral (Case B) arrives with its record already
    # resolved; reusing it keeps the one-lookup rule.
    hik_outcome = buf.get("hik_outcome")
    if hik_outcome is None:
        hik_outcome = await hikcentral.validate_entry_plate(plate, event_time, db)
    if hik_outcome.plate and hik_outcome.plate != plate:
        logger.warning(
            "[UC1] plate replaced by HikCentral: %s -> %s (guid=%s)",
            plate, hik_outcome.plate, hik_outcome.guid,
        )
        plate = hik_outcome.plate

    # dedup / anti-bounce suppress only the PMS-side occupancy record (the
    # EntryExitLog row, the open session, the alert). They must NOT suppress the
    # forward of the identity image to VA (port 8000): VA has its own dedup and
    # needs the image to (re)build the car's ReID identity + on-disk folder.
    suppress_occupancy = False
    suppress_reason = ""

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
        suppress_occupancy = True
        suppress_reason = "duplicate"
        logger.debug(
            f"[UC1] Duplicate entry — occupancy suppressed for plate={plate} "
            f"(VA still notified)"
        )

    # Anti-bounce: an entry whose plate just exited within the window is the
    # entry camera catching a car driving away from the exit gate — suppress.
    antibounce_s = settings.ENTRY_ANTIBOUNCE_SECONDS
    if not suppress_occupancy and antibounce_s > 0:
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
            suppress_occupancy = True
            suppress_reason = "anti-bounce"
            logger.info(
                "[UC1] Anti-bounce: occupancy suppressed for plate=%s "
                "(last exit %.1fs ago, window=%ds) — VA still notified",
                plate, gap_s, antibounce_s,
            )

    # Register this entry in the recent-entries cache BEFORE any network I/O, so a
    # CAM-03 crossing that fires during the forwards below (the normal ordering)
    # can already attach its image. Registered even when occupancy is suppressed,
    # so a late CAM-03 B-entry still reaches VA (the primary ReID reference).
    # sent_sources seeds with the snapshots collected so far, so a CAM-03 already
    # in confirm_snapshots is not double-sent, while a CAM-03 that arrives
    # mid-flush finds this entry instead of falling through to an older car.
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

    # HikCentral imagery is fetched only now that suppression has been decided,
    # so a duplicate or anti-bounced burst — which will never become a session —
    # never costs a download. Still above the DB writes, so no write lock is held
    # across this network await (see the deadlock note below).
    hik_images = HikImages()
    if not suppress_occupancy:
        hik_images = await hikcentral.download_hik_images(hik_outcome)

    # ── DB WRITES — must complete and COMMIT before any network await below.
    #
    # NEVER hold an uncommitted write across an `await` that does I/O. The DB
    # driver (pyodbc) is SYNCHRONOUS and runs on the asyncio event loop, so:
    # this coroutine writes entry_exit_log + parking_sessions (taking X locks),
    # awaits a PMS forward, and yields the loop — then a concurrent camera-event
    # handler runs occupancy_service's "count open parking sessions" query, which
    # needs an S lock on the row we just wrote and BLOCKS THE EVENT LOOP ITSELF.
    # The loop is what would have resumed us to commit, so the locks are never
    # released and both sides wait forever. SQL Server cannot detect it (our
    # session is idle, not waiting on SQL). This wedged the backend hard on
    # 2026-07-12 (the PMS being down stretched the forward long enough to collide
    # with the next event) and made entry_exit_log unreadable to every client.
    # Keep every network await BELOW the commit.
    # A recovered entry has no gate image of its own (_recovered_entry_buffer
    # sets snapshot_path=None outright — HikCentral proved the crossing, the
    # gate camera never fired), and an Entry-V2-style flush may carry none
    # either — fall back to HikCentral's vehicle shot so the stay is not
    # imageless on the dashboard.
    #
    # Computed HERE, above the occupancy branch, because the UC4 alert below
    # needs it too. It previously lived inside that branch and the alert used
    # the raw `snapshot_path`, so every recovered entry produced an
    # unknown_vehicle alert with a NULL snapshot while the entry_exit_log row
    # for the same car carried the Hik image — operators got an evidence-less
    # alert for a car we did in fact have a picture of.
    entry_snapshot = snapshot_path or hik_images.vehicle_image_path

    vehicle = None
    if not suppress_occupancy:
        # UC4: resolve the vehicle for the WINNING plate only — so the only
        # vehicles row this entry creates is for the final, correct plate.
        vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
        vehicle_type = vehicle.vehicle_type if vehicle else "unknown"

        log_entry = EntryExitLog(
            plate_number=plate,
            vehicle_id=vehicle.id if vehicle else None,
            vehicle_type=vehicle_type,
            gate="entry",
            camera_id=camera_id,
            event_time=event_time,
            snapshot_path=entry_snapshot,
            plate_confidence=confidence,
            created_at=facility_now_naive(),
        )
        db.add(log_entry)
        # A confirmed inward crossing PROVES any older open stay for this plate
        # is obsolete — the car cannot be inside twice — so close it here,
        # before open_session gets a chance to reuse it. Without this the stay
        # from the missed exit absorbs the new arrival and the car reads as
        # having never left (KXR-2538, 2026-08-23 06:12, stay open since 08-20).
        _reconcile_stale_stays_on_reentry(db, plate, event_time)
        session = parking_session_service.open_session(
            db,
            plate_number=plate,
            event_time=event_time,
            camera_id=camera_id,
            snapshot_path=entry_snapshot,
            vehicle=vehicle,
        )
        # open_session() flushes, so both ids are populated by now.
        hikcentral.record_hik_validation(
            db,
            outcome=hik_outcome,
            direction=hikcentral.DIRECTION_ENTRY,
            images=hik_images,
            session_id=session.id if session else None,
            entry_exit_log_id=log_entry.id,
        )
        logger.info(
            f"[UC1] Entry CONFIRMED (burst flush): plate={plate} "
            f"source={buf.get('confirm_source') or 'idle/boundary'} "
            f"plate_source={hik_outcome.plate_source}"
        )
    else:
        logger.info(
            f"[UC1] Entry occupancy suppressed ({suppress_reason}) for "
            f"plate={plate}; VA identity image forwarded"
        )

    # UC4 alert/notification — only when we actually recorded the entry above.
    # A dedup/anti-bounce-suppressed entry must not raise a fresh gate alert.
    # Safe to run pre-commit: create_alert/broadcast_event only touch the DB and
    # the in-process SSE bus (event_bus.publish → put_nowait) — no network I/O,
    # so neither can suspend this coroutine while the write locks are held.
    if suppress_occupancy:
        pass
    elif vehicle and vehicle.is_registered:
        from app.services.alert_service import broadcast_event
        await broadcast_event(
            is_alert=False,
            severity="info",
            event_type="AccessControllerEvent",
            description=f"Registered vehicle at entry gate: plate {plate}",
            camera_id=camera_id,
            zone_id="entry",
            plate_number=plate,
            snapshot_path=entry_snapshot,
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
            snapshot_path=entry_snapshot,
        )

    # Release the write locks BEFORE any network I/O (see the note above). The
    # caller commits again after all bursts; committing here too is harmless.
    db.commit()

    # ── NETWORK FORWARDS — no transaction is held from here on. ─────────────
    # Gate image first (the WINNING plate — never the wrong early read), then
    # every confirmation snapshot collected so far (CAM-23, and CAM-03 if it
    # already fired), each under its own PMS direction marker. Forwarded even
    # when occupancy was suppressed: VA has its own dedup and needs the image to
    # (re)build the car's ReID identity. (This entry was already recorded in
    # _recent_entries above, before the I/O, so a CAM-03 crossing arriving LATER
    # can still attach its image to it.)
    try:
        await core_backend_client.notify_pms_anpr(
            plate, "entry", image_path=local_snapshot or snapshot_path,
        )
    except Exception as e:
        logger.warning(f"[UC1] PMS API forwarding failed for plate={plate}: {e}")

    for src, image in confirm_snapshots.items():
        if image:
            await _forward_confirm_snapshot(plate, src, image)


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


def _spawn_confirm_forward(plate: str, source_cam: str,
                           snapshot: str | None) -> None:
    """Detach a confirmation-image forward from the caller's DB transaction.

    `confirm_entry_crossing` is called from the occupancy / line-crossing webhook
    handlers, which are still inside the request's OPEN transaction (the router
    commits after dispatch) and already hold write locks on zone_occupancy.
    Awaiting the ~10s PMS forward there would hold those locks across an await
    and can freeze the whole event loop — the same deadlock documented in
    _flush_entry_burst. Run it as a background task instead so the caller commits
    immediately. A strong reference is kept until it finishes, otherwise the task
    can be garbage-collected mid-flight."""
    task = asyncio.create_task(
        _forward_confirm_snapshot(plate, source_cam, snapshot)
    )
    _background_forwards.add(task)
    task.add_done_callback(_background_forwards.discard)


async def drain_background_forwards() -> None:
    """Wait for any detached confirmation forwards to finish.

    Called on shutdown so a clean stop doesn't drop an in-flight image, and by
    tests that need to observe the result of a detached forward.

    Late-plate rechecks are CANCELLED rather than awaited: they sit in a
    multi-second sleep by design, and a clean stop must not block on one. The
    reconcile sweep re-finds anything dropped here, which is the whole reason it
    can now adopt a late plate onto an existing exit row."""
    if _background_forwards:
        await asyncio.gather(*list(_background_forwards), return_exceptions=True)
    await exit_pipeline.drain_late_rechecks(cancel=True)


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


async def handle_anpr_event(
    event: ParsedCameraEvent,
    db: Session,
) -> Optional[AnprPostCommitForward]:
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

    if (
        gate == "exit"
        and settings.ENTRY_V2_MODE == "authoritative"
        and event.trigger_time_source.startswith("pms_receive_")
    ):
        raise SourceTimestampUnavailable(
            "exit ANPR requires a valid camera source timestamp"
        )

    # Camera sends tz-aware timestamps like `2026-05-07T12:14:51+03:00`. The
    # DB convention (since 2026-05-07) is NAIVE FACILITY-LOCAL — the wall
    # clock the operator sees, NOT UTC. Convert to facility tz first, then
    # strip tzinfo, so 12:14:51+03:00 stays 12:14:51 in the column.
    source_captured_at = event.trigger_time
    if source_captured_at is None:
        source_captured_at = facility_now_naive().replace(tzinfo=facility_tz())
    elif (
        source_captured_at.tzinfo is None
        or source_captured_at.utcoffset() is None
    ):
        # Hikvision timestamps are normally offset-aware.  A legacy/parser test
        # may still supply facility wall-clock time without tzinfo; its only
        # unambiguous interpretation in PMS is the configured facility zone.
        source_captured_at = source_captured_at.replace(tzinfo=facility_tz())
    event_time = source_captured_at.astimezone(facility_tz()).replace(tzinfo=None)

    # ── ENTRY: buffer the multi-read burst — the LAST read wins. The DB write,
    # PMS forward (port 8000) and vehicle resolution all happen once, at flush
    # time, on the winning plate. Nothing is written to the DB here. ────────
    if gate == "entry":
        await _buffer_entry_read(event, plate, event_time)
        return
    # ── The exit plate is checked by HikCentral HERE, before anything keys off
    # it. Dedup, the audit row, the session close, the alert and the VA forward
    # then all agree on one string — `plate` is that string from this line on.
    # Off/shadow/unreachable/no exit indexCode all return the edge plate, so this
    # is a no-op wherever the layer is not authoritative.
    exit_event = await exit_pipeline.from_camera_event(event, plate, event_time, db)
    plate = exit_event.plate
    exit_snapshot = exit_event.snapshot_path
    # Build the VA notification before deduplication. If PMS committed the first
    # delivery and then crashed before the post-commit forward could run or spool,
    # the camera's duplicate webhook is the only recovery signal. Returning this
    # same idempotent forward for a duplicate heals that commit-to-forward window
    # without repeating any PMS row/session/alert mutation.
    exit_forward = AnprPostCommitForward(
        plate=plate,
        direction=gate,
        image_path=event.local_snapshot_path or event.snapshot_path,
        captured_at=source_captured_at,
    )

    # ── EXIT: a single read is reliable — log immediately. ──────────────────
    # Only authoritative V2 has a second writer (the confirmation callback).
    # The transaction-owned lock remains held through the router's commit, and
    # the callback acquires the exact same normalized-plate resource. Whichever
    # transaction wins is therefore fully visible before the other decides
    # whether an open stay may exist. Off/shadow keep their legacy exit path and
    # cannot acquire/fail a lock that they do not need.
    if settings.ENTRY_V2_MODE == "authoritative":
        acquire_plate_transaction_lock(db, plate)
    # Keyed on the POST-HikCentral plate. The camera files both readings of a car
    # it read twice, and an exact-plate dedup on the edge string sees two
    # different plates and processes both — `AAA-2538` did exactly that on 8/11
    # and 8/12. Corrected first, the two collapse to one.
    logger.debug(f"[UC1] Checking dedup for plate {plate}...")
    dedup_window = event_time - timedelta(seconds=30)
    dedup_ceiling = event_time + timedelta(seconds=30)
    recent = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == gate,
            EntryExitLog.event_time >= dedup_window,
            EntryExitLog.event_time <= dedup_ceiling,
        )
        .first()
    )
    if recent:
        logger.debug(
            "[UC1] Duplicate PMS exit mutation suppressed for plate=%s gate=%s; "
            "VA exit notification will be replayed",
            plate,
            gate,
        )
        return exit_forward

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
        snapshot_path=exit_snapshot,
        created_at=facility_now_naive(),
    )

    # UC2: Calculation of Parking Duration on Exit
    matching_entry = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.plate_number == plate,
            EntryExitLog.gate == "entry",
            EntryExitLog.matched_entry_id.is_(None),
            EntryExitLog.event_time <= event_time,
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

    # Close the stay — exact plate first, then the matcher for a stay standing
    # under a misread entry. Same two steps the reconcile sweep now runs.
    exit_outcome = await exit_pipeline.resolve(
        db,
        exit_event,
        vehicle=vehicle,
        exit_image_path=event.local_snapshot_path or event.snapshot_path,
        exit_log=log_entry,
    )
    closed = exit_outcome.session
    # A stay closed under a DIFFERENT plate has no entry row this exit could
    # match, so UC2's duration query above found nothing; take it from the stay.
    if exit_outcome.corrected and log_entry.parking_duration is None:
        duration = int((event_time - closed.entry_time).total_seconds())
        log_entry.parking_duration = max(0, duration)
    # Pair the audit rows from the RESOLVED session. UC2 above searched by the
    # EXIT's plate, which is precisely the string that does not match when the
    # entry was misread — so every non-exact close left `matched_entry_id` NULL
    # and the trail could not say which entry a given exit ended.
    _pair_audit_rows(db, log_entry, closed)

    from app.services.occupancy_service import (
        reconcile_zone_counts_from_open_sessions,
    )

    reconcile_zone_counts_from_open_sessions(
        db,
        camera_id=event.camera_id,
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

    if log_entry.id is None:
        db.flush()

    # HikCentral held nothing for this pass when we asked, 2-3s after the car
    # passed. Ask once more after it has had time to ingest — detached, so the
    # gate is never made to wait for a second opinion.
    # The ledger row for a plate HikCentral corrected at the gate. Written here
    # rather than in `from_camera_event` because it needs the audit row's id, and
    # that only exists once the row is flushed. Consuming the GUID also stops the
    # reconcile sweep re-examining a pass the edge already handled.
    if exit_event.corrected_by_hik:
        hikcentral.record_hik_validation(
            db,
            outcome=exit_event.hik_outcome,
            direction=hikcentral.DIRECTION_EXIT,
            session_id=closed.id if closed is not None else None,
            entry_exit_log_id=log_entry.id,
        )

    exit_pipeline.schedule_late_plate_recheck(exit_event, log_entry.id)

    # VA is told about a correction only once it is durable. The router commits
    # immediately after this returns, and the rename is idempotent, so a detached
    # task is safe — and if it loses the race the exit sweep re-applies it.
    if exit_outcome.correction is not None:
        exit_pipeline.schedule_va_correction_notify(exit_outcome.correction)

    # Network delivery is intentionally deferred until the router commits.
    # SQL Server transaction-owned application locks are released by that
    # commit, so a slow VA call cannot block a confirmation for this plate.
    return exit_forward
