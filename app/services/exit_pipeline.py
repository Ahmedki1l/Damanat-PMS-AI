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

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import exit_match_service, parking_session_service
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

    @property
    def from_reconcile(self) -> bool:
        return self.source == SOURCE_HIK_RECONCILE


@dataclass(frozen=True)
class ExitOutcome:
    """What the pipeline did with one exit.

    `session` is the stay that was closed, or None when nothing was. `match` is
    set only when the exact-plate close missed and the matcher was consulted, so
    `match is None` means "closed on its own plate" — not "no attempt made".
    """

    session: Optional[ParkingSession] = None
    match: Optional[exit_match_service.ExitResolution] = None

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
    logger.warning(
        "[UC2] Exit %s resolved to session id=%s plate=%s via %s — %s",
        event.plate, resolution.session.id, resolution.session.plate_number,
        event.source, resolution.reason,
    )
    return ExitOutcome(session=session, match=resolution)
