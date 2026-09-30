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

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import parking_session_service
from app.services.event_parser import plate_parts as _parts, same_vehicle_plate
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
    # What the slots said about each candidate, carried so the Log X line can
    # print WHY each was or was not chosen without re-querying VA's tables.
    slot_verdicts: dict = field(default_factory=dict)

    @property
    def matched(self) -> bool:
        return self.kind == MATCHED


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

    UNBOUNDED by age since 2026-08-18. `EXIT_MATCH_MAX_AGE_HOURS=72` was meant to
    stop a long-abandoned phantom being revived by an unrelated car days later,
    but it also hid the sessions that need resolving MOST: `ABR-8000` (98h) and
    `KBD-6795` (120h) could not appear as a candidate for any exit, so they could
    never self-heal. And `close_session` never had the bound, so the two halves
    of one decision disagreed about what "open" means.

    Age is not lost, only demoted from a filter to an attribute — it is on the
    session and printed with the candidate. Nothing here is closed on a string
    anyway, so a stale candidate costs one more line in a Log X, not a wrong
    close. The pool is the same one `close_session` uses: at 35 slots it is tens
    of rows.
    """
    rows = parking_session_service.open_stays(db, exit_time)

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


# ── Slot evidence: what VA physically watched ───────────────────────────────
# Raw SQL because `parking_slots` and `slot_status` are VA-OWNED. PMS-AI has no
# model for them and should not grow one to read four columns — the same reason
# `slot_recovery_service._live_slot_state` reaches for `text()`.

ELIMINATED = "eliminated"   # this car is still in its slot; it did not leave
CONFIRMED = "confirmed"     # its slot emptied inside the drive-out window
UNKNOWN = "unknown"         # no signal — NEVER read as evidence against


@dataclass(frozen=True)
class SlotVerdict:
    """What the slot says about one candidate, and why."""

    kind: str
    reason: str
    left_at: Optional[datetime] = None


def _slot_rows(db: Session, slot_ids: Sequence[str]) -> dict:
    """Live state for each slot: (occupied, current_plate)."""
    if not slot_ids:
        return {}
    ids = list(dict.fromkeys(slot_ids))
    binds = {f"s{i}": sid for i, sid in enumerate(ids)}
    placeholders = ", ".join(f":{k}" for k in binds)
    rows = db.execute(
        text(
            "SELECT slot_id, is_available, current_plate FROM parking_slots "
            f"WHERE slot_id IN ({placeholders})"
        ),
        binds,
    ).all()
    return {r[0]: (not bool(r[1]), (r[2] or "").strip()) for r in rows}


def _slot_history(
    db: Session, slot_ids: Sequence[str], exit_time: datetime, window: timedelta
) -> dict:
    """Transitions for each slot around this exit, newest first.

    The window is [exit_time - window, exit_time + vacancy_lag] and the tail is
    NOT symmetry for its own sake: VA confirms a vacancy only after 5 frames at
    roughly 0.1 fps, so its timestamp trails the car by a measured 3.1-41.4s and
    a slot near the gate can be stamped empty after the car is already through
    the barrier.

    Bounded rather than fetched wholesale: `slot_status` is an append-only
    history and the only part of it that can speak about THIS exit is the part
    around it.
    """
    if not slot_ids:
        return {}
    ids = list(dict.fromkeys(slot_ids))
    binds = {f"s{i}": sid for i, sid in enumerate(ids)}
    placeholders = ", ".join(f":{k}" for k in binds)
    binds["lo"] = exit_time - window
    # Deliberately asymmetric. VA stamps a vacancy only after 5 confirming
    # frames at ~0.1 fps, so the timestamp lags the physical departure by a
    # measured 3.1-41.4s and can land AFTER the exit it belongs to.
    binds["hi"] = exit_time + timedelta(
        seconds=settings.EXIT_SLOT_VACANCY_LAG_SECONDS
    )
    rows = db.execute(
        text(
            "SELECT slot_id, plate_number, status, time FROM slot_status "
            f"WHERE slot_id IN ({placeholders}) "
            "AND time >= :lo AND time <= :hi ORDER BY time DESC"
        ),
        binds,
    ).all()
    out: dict = {}
    for slot_id, plate, status, when in rows:
        out.setdefault(slot_id, []).append(
            (plate or "", (status or "").strip().lower(), when)
        )
    return out


def slot_evidence(
    db: Session, candidates: Sequence, exit_time: datetime
) -> dict:
    """A verdict per candidate plate, from what VA watched in the slots.

    Four rules, and the asymmetry between them is the whole design:

      * the slot STILL shows this plate parked -> ELIMINATED. The car is inside;
        it is not the one at the gate.
      * the slot emptied inside the drive-out window -> CONFIRMED. Something left
        that slot just now, and this exit is the only departure to attach it to.
      * the slot now holds a DIFFERENT plate -> the candidate left by the
        reassignment at the latest. That is an upper bound, not a verdict: a car
        can move between slots, so this neither confirms nor eliminates. It is
        recorded so the Log X line carries it.
      * no slot, no row, no history -> UNKNOWN, and UNKNOWN is never read as
        evidence against a candidate. 15 of 35 slots run VA_IDENTITY_DISABLED on
        B2 and produce no plate signal at all; treating their silence as
        elimination would quietly make every B2 car unmatchable.

    Never raises. VA's tables are another service's, and an exit must not fail
    because a join did.
    """
    verdicts = {c.plate: SlotVerdict(UNKNOWN, "no slot on this stay")
                for c in candidates}
    if not settings.EXIT_SLOT_EVIDENCE_ENABLED:
        return {p: SlotVerdict(UNKNOWN, "slot evidence disabled") for p in verdicts}

    by_slot = {
        c.plate: getattr(c.session, "slot_id", None)
        for c in candidates
        if getattr(c.session, "slot_id", None)
    }
    if not by_slot:
        return verdicts

    window = timedelta(seconds=settings.EXIT_DRIVE_OUT_SECONDS)
    try:
        live = _slot_rows(db, list(by_slot.values()))
        history = _slot_history(db, list(by_slot.values()), exit_time, window)
    except Exception as exc:
        logger.warning("[UC2] slot evidence unavailable: %r", exc)
        return {p: SlotVerdict(UNKNOWN, "slot tables unreadable") for p in verdicts}

    for plate, slot_id in by_slot.items():
        occupied, current = live.get(slot_id, (None, ""))
        if occupied is None:
            verdicts[plate] = SlotVerdict(UNKNOWN, f"slot {slot_id} not found")
            continue

        if occupied and current and same_vehicle_plate(current, plate):
            verdicts[plate] = SlotVerdict(
                ELIMINATED, f"slot {slot_id} still shows {current} parked"
            )
            continue

        vacated = next(
            (
                when
                for candidate_plate, status, when in history.get(slot_id, ())
                if status == "available"
                or (candidate_plate and not same_vehicle_plate(candidate_plate, plate))
            ),
            None,
        )
        if vacated is not None:
            verdicts[plate] = SlotVerdict(
                CONFIRMED,
                f"slot {slot_id} emptied at {vacated}, within "
                f"{settings.EXIT_DRIVE_OUT_SECONDS:.0f}s of this exit",
                left_at=vacated,
            )
            continue

        if occupied and current:
            verdicts[plate] = SlotVerdict(
                UNKNOWN,
                f"slot {slot_id} now holds {current} — this car left by then, "
                "but a car can change slots, so that is a bound not a verdict",
            )
            continue

        verdicts[plate] = SlotVerdict(
            UNKNOWN, f"slot {slot_id} is vacant, but not within the window"
        )
    return verdicts


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

    # ── Slot evidence first. It is the only tier that can ELIMINATE, and it does
    # so on something physical: VA watched that car sitting in its slot while
    # this exit happened. Running it ahead of Re-ID means appearance is never
    # asked to choose between a car that left and a car that demonstrably did not.
    verdicts = slot_evidence(db, resolution.candidates, exit_time)
    survivors = tuple(
        c for c in resolution.candidates
        if verdicts[c.plate].kind != ELIMINATED
    )
    confirmed = [c for c in resolution.candidates
                 if verdicts[c.plate].kind == CONFIRMED]

    if len(confirmed) == 1:
        winner = confirmed[0]
        return ExitResolution(
            MATCHED,
            f"slot evidence: {verdicts[winner.plate].reason}",
            session=winner.session,
            candidates=resolution.candidates,
            slot_verdicts=verdicts,
        )
    if len(confirmed) > 1:
        # Two slots emptied in the same window. One of them belongs to this exit
        # and nothing here says which, so the tier declines and hands both on.
        logger.info(
            "[UC2] %d slots emptied within the drive-out window for %s — "
            "slot evidence declines: %s",
            len(confirmed), plate,
            "; ".join(verdicts[c.plate].reason for c in confirmed),
        )
    if not survivors:
        return ExitResolution(
            NO_CANDIDATES,
            "every candidate is still parked in its slot",
            candidates=resolution.candidates,
            slot_verdicts=verdicts,
        )
    if len(survivors) < len(resolution.candidates):
        logger.info(
            "[UC2] slot evidence eliminated %d of %d candidates for %s",
            len(resolution.candidates) - len(survivors),
            len(resolution.candidates), plate,
        )
    resolution = ExitResolution(
        resolution.kind, resolution.reason, candidates=survivors,
        slot_verdicts=verdicts,
    )

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

    # The absolute floor is opt-in: <= 0 means the margin alone decides. See
    # EXIT_MATCH_REID_MIN_SCORE for why 0.50 was dropped (it refused a correct
    # match at score 0.410 / margin 0.421 on 2026-08-20).
    score_floor = settings.EXIT_MATCH_REID_MIN_SCORE
    if margin >= settings.EXIT_MATCH_REID_MIN_MARGIN and (
        score_floor <= 0.0 or best_score >= score_floor
    ):
        return ExitResolution(
            MATCHED,
            f"appearance: {best_plate} score={best_score:.3f} margin={margin:.3f}",
            session=by_plate[best_plate].session,
            candidates=resolution.candidates,
            slot_verdicts=resolution.slot_verdicts,
        )

    logger.info(
        "[UC2] ReID inconclusive for %s — best=%s %.3f margin=%.3f (need %s"
        "margin>=%.2f)",
        plate, best_plate, best_score, margin,
        f"score>={score_floor:.2f} " if score_floor > 0.0 else "",
        settings.EXIT_MATCH_REID_MIN_MARGIN,
    )
    return ExitResolution(
        AMBIGUOUS,
        f"appearance inconclusive (best {best_score:.3f}, margin {margin:.3f})",
        candidates=resolution.candidates,
        slot_verdicts=resolution.slot_verdicts,
    )


def describe(
    resolution: ExitResolution, verdicts: Optional[dict] = None
) -> str:
    """One-line candidate summary for the log, so an operator can audit the call.

    This IS the Log X record. An exit that resolves to nothing must still leave
    behind every candidate it considered and why each was not chosen — otherwise
    "unresolved" is indistinguishable from "never looked", and the string metrics
    that are no longer allowed to DECIDE are exactly what makes the line
    readable afterwards.
    """
    if not resolution.candidates:
        return "candidates=[]"
    parts = []
    for c in resolution.candidates:
        marks = f"d={c.distance}"
        if c.digits_exact:
            marks += ",digits"
        if c.truncated:
            marks += ",trunc"
        if verdicts and c.plate in verdicts:
            marks += f",slot={verdicts[c.plate].kind}"
        parts.append(f"{c.plate}({marks})")
    return "candidates=[" + " ".join(parts) + "]"
