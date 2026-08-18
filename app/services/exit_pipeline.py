"""One path for every exit, whatever noticed it.

Two ingest paths report exits and they never converged. The edge webhook
(`handle_anpr_event`) closed the session and, when no session matched the plate,
handed the exit to `exit_match_service` to find the stay opened under a misread.
The HikCentral reconcile sweep (`_reconcile_missed_exits`) called `close_session`
and stopped there — so an exit recovered from the platform for a car whose ENTRY
plate was wrong found nothing, closed nothing, and consumed its GUID on the way
out so no later sweep would ever retry it. That is `SNA-226`: a real Hik-confirmed
entry on 8/13, an exit inside the 8/14 ingest blackout, and 75 hours on the
dashboard as an overstay.

`ExitEvent` is what both paths build. `resolve` is what both then run. Neither
path keeps a private notion of what an exit means.

Direction of travel through this module:

    ParsedCameraEvent ──from_camera_event──┐
                                           ├─→ ExitEvent ─→ resolve ─→ ExitOutcome
    HikOutcome (polled) ─from_polled_outcome┘

The caller still owns its own audit row, its own alerts and its own VA forward —
those genuinely differ between a live exit and a five-day-old recovered one.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models.entry_exit_log import EntryExitLog
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import (
    exit_match_service,
    parking_session_service,
    plate_correction_service,
    vehicle_service,
)
from app.services.hikcentral.models import HikOutcome
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Where the exit was noticed. Not cosmetic: `hik_reconcile` events carry a GUID
# the caller must consume, and their plate has already been through HikCentral,
# so `from_camera_event`'s validation would be asking the platform to check its
# own answer.
SOURCE_EDGE = "edge"
SOURCE_HIK_RECONCILE = "hik_reconcile"

# Camera id the reconciler stamps on exits it recovered from the platform. The
# edge exit camera never reported them, so attributing them to it would be a lie
# in the audit trail.
RECONCILE_CAMERA_ID = "CAM-EXIT"

# `_validate_plate`'s answer when the platform held NO pass in the window at all
# — as opposed to holding one that disagreed. The distinction is the whole basis
# of the second ask: a record that does not exist yet may exist in 15 seconds; a
# record that exists and disagrees will not change its mind.
NO_HIK_RECORD = "no_hik_record"

# Detached late-plate rechecks, kept referenced so they cannot be garbage
# collected mid-sleep. Same discipline as `_background_forwards`.
_late_rechecks: set = set()


@dataclass(frozen=True)
class ExitEvent:
    """One car leaving, normalised across both ingest paths.

    `plate` is post-HikCentral: the plate every downstream decision must use.
    Frozen because a correction applied halfway through the pipeline is exactly
    the class of bug this module exists to remove.
    """

    plate: str
    event_time: datetime
    camera_id: str
    snapshot_path: Optional[str]
    source: str
    hik_guid: Optional[str] = None
    # Why the plate is what it is. Only one value is acted on — `no_hik_record`
    # means the platform held nothing for this pass, which is the one case a
    # second ask can still change (see `schedule_late_plate_recheck`). Everything
    # else is a settled answer: it looked and disagreed, or it was never asked.
    hik_reason: Optional[str] = None

    @property
    def from_reconcile(self) -> bool:
        return self.source == SOURCE_HIK_RECONCILE

    @property
    def plate_may_still_arrive(self) -> bool:
        return self.hik_reason == NO_HIK_RECORD


@dataclass(frozen=True)
class ExitOutcome:
    """What the pipeline did with one exit.

    `session` is the stay that was closed, or None when nothing was. `match` is
    set only when the exact-plate close missed and the matcher was consulted, so
    `match is None` means "closed on its own plate" — not "no attempt made".
    """

    session: Optional[ParkingSession] = None
    match: Optional[exit_match_service.ExitResolution] = None
    # Set when the closed stay was standing under a misread and this exit
    # rewrote it. The caller fires `plate_correction_service.notify_va` for it
    # AFTER committing — VA must only ever be told about a durable correction.
    correction: Optional[plate_correction_service.Correction] = None

    @property
    def closed(self) -> bool:
        return self.session is not None

    @property
    def corrected(self) -> bool:
        """Closed a stay standing under a DIFFERENT plate than the exit read."""
        return self.session is not None and self.match is not None


async def from_camera_event(
    event,
    plate: str,
    event_time: datetime,
    db: Optional[Session] = None,
) -> ExitEvent:
    """Build the exit the edge camera just reported, plate checked by HikCentral.

    The check runs here, before the caller keys anything off the plate, because
    every later step — dedup, the audit row, the session close, the correction —
    must agree on one string. Splitting "the plate we deduped on" from "the plate
    we closed with" is how a car ends up logged twice.

    Degrades in every direction: the layer off, the platform unreachable, no exit
    indexCode configured, or shadow mode all return the edge plate unchanged.
    """
    from app.services import hikcentral

    outcome = await hikcentral.validate_exit_plate(plate, event_time, db)
    resolved = outcome.plate or plate
    if resolved != plate:
        logger.warning(
            "[Exit] HikCentral corrected the exit plate %s -> %s (%s) guid=%s",
            plate, resolved, outcome.reason, outcome.guid,
        )
    return ExitEvent(
        plate=resolved,
        event_time=event_time,
        camera_id=event.camera_id,
        snapshot_path=event.snapshot_path,
        source=SOURCE_EDGE,
        hik_guid=outcome.guid,
        hik_reason=outcome.reason,
    )


def from_polled_outcome(
    outcome: HikOutcome, snapshot_path: Optional[str] = None
) -> ExitEvent:
    """Build the exit the reconcile sweep found on the platform.

    No `validate_exit_plate` here — HikCentral IS the source of this plate, and
    asking it to confirm its own record would consume a second GUID for one car.
    """
    return ExitEvent(
        plate=outcome.plate,
        event_time=outcome.pass_time_local,
        camera_id=RECONCILE_CAMERA_ID,
        snapshot_path=snapshot_path,
        source=SOURCE_HIK_RECONCILE,
        hik_guid=outcome.guid,
    )


async def resolve(
    db: Session,
    event: ExitEvent,
    *,
    vehicle: Optional[Vehicle] = None,
    exit_image_path: Optional[str] = None,
    exit_log: Optional[EntryExitLog] = None,
) -> ExitOutcome:
    """Find and close the stay this exit ends.

    Two steps, in this order and no other:

    1. The plate closes its own open stay. This is the ~130-a-week happy path.
    2. Nothing closed. Either the plate is right and the ENTRY was lost, or the
       entry read was wrong and the stay is open under another string.
       `exit_match_service` separates those; only the second is ever matched, and
       an inconclusive answer closes nothing.

    `exit_image_path` is the local crop ReID scores against, which is not always
    `event.snapshot_path` — the edge path keeps an unpublished local file for
    exactly this. It stays an argument rather than a field so the frozen event
    describes the car, not the machinery.

    `exit_log` is this exit's audit row, when the caller has already written one,
    so a correction can carry it along. Nothing commits here: the ledger row, the
    session rewrite and the caller's own row all land or fail together.
    """
    session = parking_session_service.close_session(
        db,
        plate_number=event.plate,
        event_time=event.event_time,
        camera_id=event.camera_id,
        snapshot_path=event.snapshot_path,
    )
    if session is not None:
        return ExitOutcome(session=session)

    resolution = await exit_match_service.resolve_with_appearance(
        db, event.plate, event.event_time, vehicle, exit_image_path,
    )
    if not resolution.matched:
        logger.warning(
            "[UC2] Exit %s unresolved (%s) via %s: %s | %s",
            event.plate, resolution.kind, event.source, resolution.reason,
            exit_match_service.describe(resolution),
        )
        return ExitOutcome(match=resolution)

    session = parking_session_service.close_matched_session(
        db,
        resolution.session,
        exit_time=event.event_time,
        camera_id=event.camera_id,
        snapshot_path=event.snapshot_path,
    )
    if session is None:
        # The matcher named a stay and the close refused it. Unreachable while
        # `_close_session_record` returns the row unconditionally; it opens the
        # moment the close is guarded (`UPDATE ... WHERE status='open'`), which
        # is how a stale read is stopped from double-closing a stay another
        # writer already ended. Reported as unclosed rather than logged as a
        # success that did not happen — `ExitOutcome.corrected` is what gates
        # the caller's duration backfill, and a stay that never closed has no
        # exit to measure to.
        logger.warning(
            "[UC2] Exit %s matched session id=%s plate=%s via %s but the close "
            "was refused — the stay is left open",
            event.plate, resolution.session.id, resolution.session.plate_number,
            event.source,
        )
        return ExitOutcome(match=resolution)

    logger.warning(
        "[UC2] Exit %s resolved to session id=%s plate=%s via %s — %s",
        event.plate, resolution.session.id, resolution.session.plate_number,
        event.source, resolution.reason,
    )
    # The stay was opened under a misread and this exit is the first independent
    # read of the car. Correct it in the SAME transaction as the close: a stay
    # closed but not renamed leaves the dashboard showing a car that is not here.
    correction = plate_correction_service.apply_correction(
        db, session, event.plate, resolution.reason,
        exit_log=exit_log, hik_guid=event.hik_guid,
    )
    return ExitOutcome(session=session, match=resolution, correction=correction)


# ── a plate that arrived after we asked ─────────────────────────────────────


def exit_row_for_late_pass(
    db: Session, pass_time: datetime, window: timedelta
) -> Optional[EntryExitLog]:
    """The exit audit row a HikCentral pass belongs to, matched on TIME alone.

    Callers reach this only after a plate match has already failed, so what it
    answers is: "the platform says a car left at 09:00 and we logged a car
    leaving at 09:00 under a different string — are they the same car?"

    Usually yes, and the disagreement is the misread this pass exists to correct.
    Which is exactly why the sweep could not see it: `same_vehicle_plate` ties
    truncations together, not two genuinely different strings, so a corrected
    plate looked like a car nobody had logged and earned a SECOND exit row for
    one car leaving once.

    Requires EXACTLY ONE row in the window, the same discipline
    `recover_entry_plate` uses: with two cars in the window nothing says which of
    them this pass belongs to, and attaching a stranger's plate to an exit is
    worse than the duplicate row. A row already backed by another HikCentral pass
    is likewise off limits — one pass, one row.
    """
    rows = (
        db.query(EntryExitLog)
        .filter(
            EntryExitLog.gate == "exit",
            EntryExitLog.event_time >= pass_time - window,
            EntryExitLog.event_time <= pass_time + window,
        )
        .all()
    )
    if len(rows) != 1:
        if rows:
            logger.info(
                "[Exit] %d exit rows within %s of %s — too ambiguous to adopt a "
                "late plate; treating the pass as unlogged",
                len(rows), window, pass_time,
            )
        return None

    row = rows[0]
    already_backed = (
        db.query(HikValidation.id)
        .filter(HikValidation.entry_exit_log_id == row.id)
        .first()
    )
    if already_backed:
        logger.info(
            "[Exit] exit row id=%s already carries a HikCentral pass — not "
            "adopting a second one",
            row.id,
        )
        return None
    return row


def adopt_late_plate(db: Session, row: EntryExitLog, plate: str) -> Optional[str]:
    """Rewrite one exit audit row to the plate HikCentral eventually reported.

    Returns the misread it replaced, or None when there was nothing to change.

    Scope is deliberately ONE row. The stay this exit closed may still stand
    under the misread, and rewriting that is a different operation — ledger row,
    vehicle merge, VA gallery rename — which `plate_correction_service` owns.
    Doing half of it here would leave VA re-minting the wrong plate while the
    audit trail claimed it was fixed.
    """
    misread = row.plate_number
    if not plate or misread == plate:
        return None

    vehicle = vehicle_service.ensure_unregistered_vehicle(db, plate)
    row.plate_number = plate
    if vehicle is not None:
        row.vehicle_id = vehicle.id
        row.vehicle_type = vehicle.vehicle_type
    logger.warning(
        "[Exit] late HikCentral plate adopted on exit row id=%s: %s -> %s",
        row.id, misread, plate,
    )
    return misread


def schedule_late_plate_recheck(event: ExitEvent, exit_log_id: Optional[int]) -> None:
    """Ask HikCentral once more, later, when it had nothing the first time.

    The exit path asks at ~2-3s after the pass. Every successful lookup in
    ai-logs.txt landed 7-44s after its pass (p50 12s) because the entry path
    waits for a crossing and a debounce first — so an empty answer at 2s is not
    evidence the platform will never have the record, only that it does not have
    it yet. Detached rather than awaited: the gate must not wait 15s for a second
    opinion on a plate the exit camera already read correctly 97% of the time.
    """
    if not event.plate_may_still_arrive:
        return
    if settings.EXIT_HIK_RECHECK_SECONDS <= 0:
        return
    task = asyncio.create_task(_recheck_late_plate(event, exit_log_id))
    _late_rechecks.add(task)
    task.add_done_callback(_late_rechecks.discard)


async def drain_late_rechecks(cancel: bool = False) -> None:
    """Wait for detached rechecks. `cancel` at shutdown — a clean stop must not
    block for the recheck delay, and the reconcile sweep is the durable path for
    anything dropped here."""
    if not _late_rechecks:
        return
    tasks = list(_late_rechecks)
    if cancel:
        for task in tasks:
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _recheck_late_plate(event: ExitEvent, exit_log_id: Optional[int]) -> None:
    """One deferred lookup, on its own session. Never raises into the loop."""
    from app.database import SessionLocal
    from app.services import hikcentral

    await asyncio.sleep(settings.EXIT_HIK_RECHECK_SECONDS)

    db = SessionLocal()
    try:
        outcome = await hikcentral.validate_exit_plate(
            event.plate, event.event_time, db
        )
        if not outcome.matched or not outcome.plate or outcome.plate == event.plate:
            logger.info(
                "[Exit] late recheck for %s at %s: %s — nothing to correct",
                event.plate, event.event_time, outcome.reason,
            )
            return

        logger.warning(
            "[Exit] late HikCentral plate for the %s exit: %s -> %s (%s) — the "
            "platform had no record when the car left",
            event.event_time, event.plate, outcome.plate, outcome.reason,
        )
        corrected = ExitEvent(
            plate=outcome.plate,
            event_time=event.event_time,
            camera_id=event.camera_id,
            snapshot_path=event.snapshot_path,
            source=event.source,
            hik_guid=outcome.guid,
            hik_reason=outcome.reason,
        )
        # The stay may still be open under the correct plate — the edge closed
        # nothing, or closed the wrong thing. Re-running the resolution is the
        # whole value of asking again.
        result = await resolve(db, corrected, exit_image_path=event.snapshot_path)

        row = db.get(EntryExitLog, exit_log_id) if exit_log_id else None
        if row is not None:
            adopt_late_plate(db, row, outcome.plate)
        # Consume the GUID so the reconcile sweep does not redo this pass and
        # write the duplicate exit row this whole path exists to avoid.
        hikcentral.record_hik_validation(
            db,
            outcome=outcome,
            direction=hikcentral.DIRECTION_EXIT,
            session_id=result.session.id if result.session else None,
            entry_exit_log_id=row.id if row is not None else None,
        )
        db.commit()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[Exit] late plate recheck failed for %s at %s: %r",
            event.plate, event.event_time, exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        db.close()


def schedule_va_correction_notify(correction) -> None:
    """Push a committed correction to VA without blocking the caller.

    The edge path's transaction is committed by the ROUTER, after
    `handle_anpr_event` returns, so there is no point here at which this can be
    awaited in the right order. Detaching is the honest trade: the rename is
    idempotent, VA being briefly stale is harmless, and the reconcile sweep
    re-applies a correction whose notification was lost. The sweep, which owns
    its own transaction, calls `notify_va` directly after its commit instead.
    """
    if correction is None:
        return
    from app.services import plate_correction_service

    task = asyncio.create_task(plate_correction_service.notify_va(correction))
    _late_rechecks.add(task)
    task.add_done_callback(_late_rechecks.discard)
