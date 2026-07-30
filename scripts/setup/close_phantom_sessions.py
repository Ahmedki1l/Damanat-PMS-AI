"""Close parking sessions that no car can ever close.

WHY THIS EXISTS
---------------
`_reconcile_missed_entries` used to re-open every entry the ramp-crossing gate
had deliberately refused: a refused burst writes no `EntryExitLog` on purpose,
and that is exactly the state the sweep reads as "the edge missed this car". The
sessions it opened describe cars that never entered, so no exit will ever match
them. They sit `status='open'` forever and the dashboard reports them as
overstays (8 of them on 2026-07-30).

The leak itself is fixed (`hikcentral.consume_refused_entry`). This script is the
one-off cleanup of the rows already stranded by it.

SAFETY
------
NOT every open overnight session is a phantom — a car that genuinely stayed the
night looks identical in the `status` column alone. So this script never guesses:
it gathers evidence per session, classifies, and by default CHANGES NOTHING.

    python scripts/setup/close_phantom_sessions.py              # report only
    python scripts/setup/close_phantom_sessions.py --apply      # close PHANTOMs
    python scripts/setup/close_phantom_sessions.py --apply --id 41 --id 55

    # Close named cars outright, whatever the evidence says:
    python scripts/setup/close_phantom_sessions.py --apply \
        --plate BHD-9990 --plate 4941-NNR

`--plate` exists because the evidence below is only as good as the sensors, and
on B2 there are none: no OCR and no ReID run on that floor, so `parked_slot` is
always NULL there and a genuinely-departed B2 car can look like neither a clear
PHANTOM nor a clear REAL. An operator who can see the bay is a better witness
than a sensor that is switched off, so a named plate overrides the classifier.
Either spelling works — `BHD-9990` as the DB stores it, or `9990-BHD` as the
RTL dashboard renders it. Naming a plate also ignores the overstay window, so it
closes a car that came in this morning.

Evidence that a session is REAL (never auto-closed):
  * VA currently shows the plate parked in a slot (`parking_slots.current_plate`)
  * an `entry_exit_log` entry row backs it (a real car came through the gate)

  NOTE `parking_slots.current_plate` is VA-owned and CAN be stale: an unlocked
  binding restored at startup is a memory, not an observation (the B3 /
  ERS-7949 case, 2026-07-30). So this signal can wrongly protect a phantom whose
  plate happens to be bound to some slot. That error is deliberate and one-way —
  it leaves a session open rather than closing a car that is really parked. Only
  the first is recoverable: re-run the script.

Evidence that a session is a PHANTOM:
  * its `hik_validations` row says `plate_source='hik_polled'` (opened by the
    reconciler, never confirmed by a crossing), and/or
  * no entry gate-log row backs it, and/or
  * the plate never normalised (e.g. '66565EK' — 5 digits + 2 letters matches no
    Saudi pattern, so `normalize_plate` returned it raw while every real plate is
    stored 'XXX-NNNN'). Such a plate cannot match a normalised exit either.

Closes through `_close_session_record`, the same path a real exit uses, so
`duration_seconds`, `slot_left_at` and the vehicle's location all end up
consistent. Zone counts self-correct: `reconcile_zone_counts_from_open_sessions`
recomputes GARAGE-TOTAL from open sessions on the next gate event, and heals B2
down if it now exceeds the total.
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from sqlalchemy import text  # noqa: E402

from app.config import facility_now_naive, facility_today_utc  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.parking_session import ParkingSession  # noqa: E402
from app.services.parking_session_service import _close_session_record  # noqa: E402

# The exit_camera_id stamped on an administratively closed stay, so these are
# distinguishable from a real gate exit forever after.
CLEANUP_CAMERA_ID = "ADMIN-CLEANUP"

# What a normalised Saudi plate looks like once normalize_plate() has had it.
_NORMALISED = re.compile(r"^[A-Z]{2,3}-\d{1,4}$")


def _plate_key(plate: str) -> str:
    """Order-independent identity for a plate string.

    The same car is spelled two ways depending on where you read it: the DB and
    the logs store letters-first ('BHD-9990'), the Angular UI renders it
    digits-first for RTL ('9990-BHD'). An operator copying a plate off the
    dashboard types the second. Keying on (letters, digits) accepts both without
    anyone having to know which surface they were looking at.
    """
    raw = "".join(ch for ch in (plate or "").upper() if ch.isalnum())
    letters = "".join(ch for ch in raw if ch.isalpha())
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"{letters}{digits}"


def _evidence(db, s: ParkingSession) -> dict:
    """Everything known about whether this session describes a real car."""
    parked_now = db.execute(
        text(
            "SELECT slot_id FROM parking_slots WHERE current_plate = :p"
        ),
        {"p": s.plate_number},
    ).first()

    entry_logged = db.execute(
        text(
            "SELECT TOP 1 id FROM entry_exit_log "
            "WHERE plate_number = :p AND gate = 'entry' "
            "AND event_time BETWEEN DATEADD(minute, -5, :t) AND DATEADD(minute, 5, :t)"
        ),
        {"p": s.plate_number, "t": s.entry_time},
    ).first()

    plate_source = db.execute(
        text(
            "SELECT TOP 1 plate_source FROM hik_validations "
            "WHERE session_id = :sid ORDER BY id DESC"
        ),
        {"sid": s.id},
    ).scalar()

    return {
        "parked_slot": parked_now[0] if parked_now else None,
        "entry_logged": entry_logged is not None,
        "plate_source": plate_source,
        "plate_odd": not bool(_NORMALISED.match(s.plate_number or "")),
    }


def _classify(ev: dict) -> tuple[str, str]:
    """(verdict, why). REAL wins over every phantom signal — closing a car that
    is actually sitting in the garage is the one unrecoverable mistake here."""
    if ev["parked_slot"]:
        return "REAL", f"VA shows it parked in {ev['parked_slot']} right now"
    if ev["plate_source"] == "hik_polled":
        return "PHANTOM", "opened by HIK-RECON, never crossing-confirmed"
    if not ev["entry_logged"]:
        return "PHANTOM", "no entry gate-log row backs this session"
    if ev["plate_odd"]:
        return "PHANTOM", "plate never normalised - cannot match a normal exit"
    return "REAL", "backed by a real entry gate-log row"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually close them (default: report only)")
    ap.add_argument("--id", type=int, action="append", default=[],
                    help="restrict to these session ids (repeatable)")
    ap.add_argument("--plate", action="append", default=[], metavar="PLATE",
                    help="close these plates outright, whatever the verdict "
                         "(repeatable). Accepts either spelling: BHD-9990 or "
                         "9990-BHD as the UI shows it.")
    ap.add_argument("--include-real", action="store_true",
                    help="also close sessions classified REAL — needs --id")
    args = ap.parse_args()

    if args.include_real and not args.id:
        print("--include-real requires an explicit --id list. Refusing.")
        return 2

    # An operator naming a plate outright is a human witness, and on B2 it is the
    # ONLY witness there is: no OCR and no ReID run on that floor, so the
    # REAL/PHANTOM evidence below is all blank there and would keep every B2 row
    # open forever. A named plate therefore overrides the classifier.
    wanted_plates = {_plate_key(p) for p in args.plate}

    db = SessionLocal()
    try:
        cutoff = facility_today_utc()
        base = db.query(ParkingSession).filter(
            ParkingSession.status.in_(("open", "overstay"))
        )

        # The overstay cutoff scopes the SWEEP, not a named plate: an operator who
        # types a plate means that car, whether or not it has been in since before
        # midnight. So named plates are fetched without the cutoff and merged in.
        q = base.filter(ParkingSession.entry_time < cutoff)
        if args.id:
            q = q.filter(ParkingSession.id.in_(args.id))
        sessions = {s.id: s for s in q.all()}
        swept = len(sessions)
        if wanted_plates:
            for s in base.all():
                if _plate_key(s.plate_number) in wanted_plates:
                    sessions[s.id] = s
        sessions = sorted(sessions.values(), key=lambda s: (s.entry_time, s.id))

        print(f"Overstaying open sessions (entry_time < {cutoff}): {swept}")
        if wanted_plates:
            print(f"Named plates: {', '.join(sorted(args.plate))} "
                  f"(+{len(sessions) - swept} outside the overstay window)")
        print()
        if not sessions:
            if wanted_plates:
                print("No OPEN session matches those plates — already closed, or "
                      "check the spelling (either direction is accepted).")
            return 0

        print(f"{'id':>5}  {'plate':<12} {'entry_time':<20} {'verdict':<8} why")
        print("-" * 100)

        to_close = []
        matched_keys = set()
        verdicts: dict[int, str] = {}
        for s in sessions:
            key = _plate_key(s.plate_number)
            if key in wanted_plates:
                matched_keys.add(key)
                verdict, why = "NAMED", "named on the command line by an operator"
            else:
                verdict, why = _classify(_evidence(db, s))
            print(f"{s.id:>5}  {s.plate_number or '':<12} "
                  f"{str(s.entry_time):<20} {verdict:<8} {why}")
            if verdict in ("PHANTOM", "NAMED") or (
                args.include_real and s.id in args.id
            ):
                verdicts[s.id] = verdict
                to_close.append(s)

        print("-" * 100)
        for missing in sorted(wanted_plates - matched_keys):
            print(f"WARNING: no open session found for plate {missing!r}")
        print(f"\n{len(to_close)} session(s) would be closed, "
              f"{len(sessions) - len(to_close)} left open.")

        if not args.apply:
            print("\nDRY RUN — nothing changed. Re-run with --apply to close them.")
            return 0

        now = facility_now_naive()
        for s in to_close:
            # The exit stamp differs by what we are actually asserting, and the
            # difference is not cosmetic — `duration_seconds` feeds the average
            # parking time on the dashboard.
            #
            #   PHANTOM — the car never entered. There was no stay, so closing at
            #     the entry instant records a zero-length one. Anything later would
            #     invent time the car never spent here and inflate the average.
            #   NAMED   — the operator is asserting a REAL car has gone; we just
            #     never saw it leave. Zeroing that would erase a stay that did
            #     happen, so close at now: the exit moment is unknown but bounded
            #     above by this instant.
            exit_time = now if verdicts.get(s.id) == "NAMED" else s.entry_time
            _close_session_record(
                db, s,
                exit_time=exit_time,
                camera_id=CLEANUP_CAMERA_ID,
                snapshot_path=None,
                clear_vehicle_location=True,
            )
            s.updated_at = now
        db.commit()
        named = sum(1 for v in verdicts.values() if v == "NAMED")
        print(f"\nClosed {len(to_close)} session(s) as {CLEANUP_CAMERA_ID} "
              f"({named} named, {len(to_close) - named} phantom).")
        print("Zone counts recompute from open sessions on the next gate event.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
