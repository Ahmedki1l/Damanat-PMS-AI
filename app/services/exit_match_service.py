"""Resolve an exit that matched no open session.

WHY THIS EXISTS
---------------
`close_session` matches on exact plate equality. When the entry LPR misreads a
plate the session opens under one string and the car leaves under another, so
nothing matches: the exit is logged unmatched and the entry session stays open
until that car happens to come back. Measured on production logs 2026-08-05..09:
KXR-2538 entered at 11:18 and left as AAB-2538 at 15:50 — same four digits,
three different letters, one car.

THE FIRST QUESTION IS NOT "WHICH SESSION"
-----------------------------------------
It is "is this plate even wrong?", and `vehicles` answers it without touching a
single session:

  * `is_registered` — a human deliberately entered this plate. An OCR error does
    not land exactly on a registered plate.
  * `current_slot_id` / `floor` — written by VA on every track confirmation. Set
    means VA has been watching this car parked INSIDE, under this plate.

Either one means the read is RIGHT and the entry was lost (a silent entry, a
burst the crossing gate refused, a dropped webhook). Matching such an exit
against another car's session would close a stranger's stay — precisely the
damage this module exists to prevent — so those exits return early and are never
matched.

Existence in `vehicles` proves nothing by itself: `ensure_unregistered_vehicle`
mints a placeholder row for any unknown plate, including during this very exit
(DJB-4541 got one created while its own exit was being processed). The flags are
the signal, never the row.

WHAT A WRONG ANSWER COSTS
-------------------------
Closing the wrong session corrupts TWO stays: the real car stays "inside"
forever and an innocent one is marked gone, silently. So every rule here is
unique-or-refuse, and a refusal is a normal outcome rather than a failure.

STRINGS NEVER DECIDE (2026-08-18)
---------------------------------
This module used to close a session when the exit plate shared its digit group,
or was a truncation of, exactly one open stay. That is gone. Two REAL cars can
differ by a single letter or digit, so a unique string match says only that no
other such car was parked at that moment — it is a property of the day's plate
pool, not evidence about this car. The rule was right about KXR-2538 twice and
had no way to know it.

What is left decides on physical evidence: HikCentral corrects the exit plate at
the source (so the exact match simply works), slot history eliminates candidates
that cannot have left, and appearance scores the rest. An exit no evidence can
place is logged with its candidates and closes nothing.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.config import settings
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services.event_parser import same_vehicle_plate
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Resolution kinds. `ENTRY_LOST` is deliberately terminal — see the module
# docstring: those exits must never reach the matcher.
ENTRY_LOST = "entry_lost"
MATCHED = "matched"
AMBIGUOUS = "ambiguous"
NO_CANDIDATES = "no_candidates"
DISABLED = "disabled"


@dataclass(frozen=True)
class Candidate:
    """One open session this exit might belong to, with the evidence behind it."""

    session: ParkingSession
    plate: str
    distance: int          # letters + digits edit distance from the exit plate
    digits_exact: bool     # same digit group, long enough to be meaningful
    truncated: bool        # same letters, one digit group a prefix/suffix of the other

    @property
    def session_id(self) -> Optional[int]:
        return getattr(self.session, "id", None)


@dataclass(frozen=True)
class ExitResolution:
    """What to do with an unmatched exit. `session` is set only for MATCHED."""

    kind: str
    reason: str
    session: Optional[ParkingSession] = None
    candidates: Sequence[Candidate] = field(default_factory=tuple)

    @property
    def matched(self) -> bool:
        return self.kind == MATCHED


def _parts(plate: Optional[str]) -> tuple:
    """(letters, digits) for a plate, order-independent.

    The DB stores letters-first (`BHD-9990`) while some OCR paths report
    digits-first (`9990BHD`); splitting by character class compares both
    spellings without accepting a different car.
    """
    raw = "".join(c for c in (plate or "").upper() if c.isalnum())
    return (
        "".join(c for c in raw if c.isalpha()),
        "".join(c for c in raw if c.isdigit()),
    )


def _edit_distance(a: str, b: str) -> int:
    """Plain Levenshtein. Inputs are <=8 chars, so the naive DP is free."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return previous[-1]


def _entry_is_known_lost(vehicle: Optional[Vehicle]) -> Optional[str]:
    """Why this exit's plate should be trusted, or None if it should not be.

    Returns the reason string so the caller can log WHICH signal fired — the two
    are diagnostically different: `registered` means a human vouched for the
    plate, `located` means VA physically watched the car inside.
    """
    if vehicle is None:
        return None
    if getattr(vehicle, "is_registered", False):
        return "registered_vehicle"
    if getattr(vehicle, "current_slot_id", None):
        return f"va_located_slot={vehicle.current_slot_id}"
    if getattr(vehicle, "floor", None):
        return f"va_located_floor={vehicle.floor}"
    return None


def find_candidates(
    db: Session, plate: str, exit_time: datetime
) -> list:
    """Open sessions this exit could plausibly belong to, best first.

    Bounded by `EXIT_MATCH_MAX_AGE_HOURS` so a long-abandoned phantom cannot be
    revived by an unrelated car days later, and by `entry_time <= exit_time`
    because a car cannot leave before it arrived.
    """
    oldest = exit_time - timedelta(hours=settings.EXIT_MATCH_MAX_AGE_HOURS)
    rows = (
        db.query(ParkingSession)
        .filter(
            ParkingSession.status == "open",
            ParkingSession.entry_time <= exit_time,
            ParkingSession.entry_time >= oldest,
        )
        .all()
    )

    exit_letters, exit_digits = _parts(plate)
    min_digits = settings.EXIT_MATCH_MIN_DIGITS
    candidates = []
    for row in rows:
        row_plate = row.plate_number
        if not row_plate or row_plate == plate:
            continue        # an exact match would have closed already
        letters, digits = _parts(row_plate)
        candidates.append(
            Candidate(
                session=row,
                plate=row_plate,
                distance=(
                    _edit_distance(exit_letters, letters)
                    + _edit_distance(exit_digits, digits)
                ),
                digits_exact=(
                    bool(digits)
                    and digits == exit_digits
                    and len(digits) >= min_digits
                ),
                truncated=same_vehicle_plate(row_plate, plate),
            )
        )
    candidates.sort(key=lambda c: (c.distance, c.plate))
    return candidates


def resolve_unmatched_exit(
    db: Session,
    plate: str,
    exit_time: datetime,
    vehicle: Optional[Vehicle] = None,
) -> ExitResolution:
    """Decide what an exit with no open session actually is.

    Order matters. The `vehicles` check runs FIRST and is terminal: a plate we
    have independent reason to trust is not a misread, so there is nothing to
    match and looking would only risk closing a stranger's session.
    """
    if not settings.EXIT_MATCH_ENABLED:
        return ExitResolution(DISABLED, "exit matching disabled")

    trusted = _entry_is_known_lost(vehicle)
    if trusted:
        return ExitResolution(
            ENTRY_LOST,
            f"plate trusted ({trusted}) — the ENTRY was lost, not the identity",
        )

    candidates = find_candidates(db, plate, exit_time)
    if not candidates:
        return ExitResolution(NO_CANDIDATES, "no open session could match")

    shortlist = tuple(candidates[: settings.EXIT_MATCH_SHORTLIST])

    # NO STRING RULE MAY CLOSE A SESSION. Two real cars can differ by one letter
    # or one digit, so "the digit group matches only this candidate" is a
    # statement about the plate pool that happened to be parked, not evidence
    # that these are the same car — and being wrong closes a stranger's stay and
    # then renames it.
    #
    # Measured over ai-logs.txt (8/10-8/16, 130 exits): the digit-group rule
    # fired twice, both times on ONE car (AAA-2538 -> KXR-2538, 8/11 and 8/12),
    # whose three letters were all wrong and whose digits happened to be unique
    # that day. The truncation rule never fired at all. That same car is now
    # corrected at the source by `validate_exit_plate`, so this rule's entire
    # observed contribution is covered by evidence instead of by coincidence.
    #
    # Candidates still carry `distance` / `digits_exact` / `truncated` — they are
    # printed in the Log X line so an operator can audit the call. They are
    # DIAGNOSTICS. Nothing here may branch on them again.
    return ExitResolution(
        AMBIGUOUS,
        f"{len(shortlist)} plausible candidates — needs physical evidence",
        candidates=shortlist,
    )


async def resolve_with_appearance(
    db: Session,
    plate: str,
    exit_time: datetime,
    vehicle: Optional[Vehicle],
    exit_image_path: Optional[str],
) -> ExitResolution:
    """`resolve_unmatched_exit`, with VA's appearance model breaking the ties.

    The plate rules only recognise shapes we have already seen — letters wrong,
    or digits truncated. A car misread in BOTH fields matches no rule at all, and
    no amount of string logic will ever reach it; appearance is the only evidence
    left. So ReID runs exactly where the deterministic rules gave up, and never
    to second-guess a rule that fired.

    Every failure — VA down, no crop, a poor query image, no gallery refs, an
    inconclusive margin — degrades to the AMBIGUOUS answer this would have
    returned anyway. Nothing is closed on weak evidence.
    """
    resolution = resolve_unmatched_exit(db, plate, exit_time, vehicle)
    if resolution.kind != AMBIGUOUS or not resolution.candidates:
        return resolution
    if not settings.EXIT_MATCH_REID_ENABLED or not exit_image_path:
        return resolution

    from app.utils import va_reid_client

    by_plate = {c.plate: c for c in resolution.candidates}
    payload = await va_reid_client.compare(exit_image_path, list(by_plate))
    if not payload:
        return resolution

    if not payload.get("query_quality_ok", True):
        logger.warning(
            "[UC2] ReID declined for %s — exit crop unusable (sharpness=%s)",
            plate, payload.get("query_sharpness"),
        )
        return resolution

    scored = [
        (r.get("plate"), float(r["score"]))
        for r in payload.get("results") or ()
        if r.get("score") is not None and r.get("plate") in by_plate
    ]
    if not scored:
        return resolution
    scored.sort(key=lambda item: -item[1])

    best_plate, best_score = scored[0]
    runner_up = scored[1][1] if len(scored) > 1 else 0.0
    margin = best_score - runner_up

    if (
        best_score >= settings.EXIT_MATCH_REID_MIN_SCORE
        and margin >= settings.EXIT_MATCH_REID_MIN_MARGIN
    ):
        return ExitResolution(
            MATCHED,
            f"appearance: {best_plate} score={best_score:.3f} margin={margin:.3f}",
            session=by_plate[best_plate].session,
            candidates=resolution.candidates,
        )

    logger.info(
        "[UC2] ReID inconclusive for %s — best=%s %.3f margin=%.3f "
        "(need score>=%.2f margin>=%.2f)",
        plate, best_plate, best_score, margin,
        settings.EXIT_MATCH_REID_MIN_SCORE,
        settings.EXIT_MATCH_REID_MIN_MARGIN,
    )
    return ExitResolution(
        AMBIGUOUS,
        f"appearance inconclusive (best {best_score:.3f}, margin {margin:.3f})",
        candidates=resolution.candidates,
    )


def describe(resolution: ExitResolution) -> str:
    """One-line candidate summary for the log, so an operator can audit the call."""
    if not resolution.candidates:
        return "candidates=[]"
    parts = [
        f"{c.plate}(d={c.distance}"
        f"{',digits' if c.digits_exact else ''}"
        f"{',trunc' if c.truncated else ''})"
        for c in resolution.candidates
    ]
    return "candidates=[" + " ".join(parts) + "]"
