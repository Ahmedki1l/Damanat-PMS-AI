"""Resolving an exit that matched no open session.

The invariant under test throughout: an exit whose plate we have independent
reason to trust is NEVER matched against another car's session, and an ambiguous
exit closes nothing at all.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import exit_match_service as ems

EXIT_TIME = datetime(2026, 8, 5, 15, 50, 47)

engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


def open_session(db, plate, *, hours_ago=4.0):
    entered = EXIT_TIME - timedelta(hours=hours_ago)
    row = ParkingSession(
        plate_number=plate,
        entry_time=entered,
        entry_camera_id="CAM-ENTRY",
        status="open",
        vehicle_type="unknown",
        created_at=entered,
        updated_at=entered,
    )
    db.add(row)
    db.flush()
    return row


def vehicle(*, registered=False, slot=None, floor=None):
    return Vehicle(
        plate_number="X",
        owner_name="",
        title="",
        vehicle_type="unknown",
        is_registered=registered,
        current_slot_id=slot,
        floor=floor,
    )


# ── The vehicles check runs first and is terminal ────────────────────────────


@pytest.mark.parametrize(
    ("veh", "why"),
    [
        (vehicle(registered=True), "a human vouched for the plate"),
        (vehicle(slot="B1-07"), "VA watched it parked in a slot"),
        (vehicle(floor="B1"), "VA watched it on a floor"),
    ],
)
def test_a_trusted_plate_is_never_matched(db, veh, why):
    """A trusted plate means the ENTRY was lost, not the identity. Matching it
    against another car's session would close a stranger's stay."""
    open_session(db, "KXR-2538")   # a perfect digit match sits right there

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, veh)

    assert out.kind == ems.ENTRY_LOST, why
    assert out.session is None


def test_a_placeholder_row_does_not_count_as_trust(db):
    """ensure_unregistered_vehicle mints a row for any unknown plate — including
    during this very exit. The flags are the signal, never the row's existence.

    A plain placeholder must NOT terminate the match as `entry_lost`: the exit
    still reaches the candidate pool, where physical evidence decides.
    """
    open_session(db, "KXR-2538")

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None
    assert [c.plate for c in out.candidates] == ["KXR-2538"]


# ── Deterministic matching ───────────────────────────────────────────────────


def test_a_unique_digit_group_no_longer_closes_anything(db):
    """2026-08-05: KXR-2538 entered 11:18, left as AAB-2538 at 15:50 — and this
    rule used to close it on the strength of the shared digits alone.

    It is a statement about the plate pool parked that day, not about this car.
    Two REAL cars can share a digit group, and being wrong closes a stranger's
    stay and then renames it. The candidate survives for slot and appearance
    evidence to judge; the string does not get a vote.
    """
    open_session(db, "KXR-2538")
    open_session(db, "HVA-77")
    open_session(db, "GLD-5965")

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert not out.matched
    assert out.kind == ems.AMBIGUOUS
    assert out.session is None
    assert "KXR-2538" in [c.plate for c in out.candidates], (
        "removing the RULE must not remove the CANDIDATE — evidence still needs it"
    )


def test_a_truncated_entry_plate_no_longer_closes_anything(db):
    """The other observed shape: entered as KKR-4, leaves as KKR-6294.

    A prefix relationship is no safer than a shared digit group: KKR-629 and
    KKR-6294 can be two cars. Never fired once in the 8/10-8/16 window.
    """
    open_session(db, "KKR-4")
    open_session(db, "GLD-5965")

    out = ems.resolve_unmatched_exit(db, "KKR-6294", EXIT_TIME, vehicle())

    assert not out.matched
    assert out.session is None
    assert "KKR-4" in [c.plate for c in out.candidates]


def test_two_digit_matches_refuse_rather_than_guess(db):
    """A tiebreak here would be a guess, and a wrong close corrupts two stays."""
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None
    assert len(out.candidates) == 2


def test_short_digit_groups_do_not_nominate(db):
    """Plenty of real plates share one or two digits."""
    open_session(db, "ZZT-77")

    out = ems.resolve_unmatched_exit(db, "HVA-77", EXIT_TIME, vehicle())

    assert out.kind != ems.MATCHED


def test_unrelated_plates_produce_no_match(db):
    """BHD-9990 was never detected at either gate — there is nothing to match,
    and inventing a pairing would close an innocent session."""
    open_session(db, "GLD-5965")
    open_session(db, "HGD-2926")

    out = ems.resolve_unmatched_exit(db, "BHD-9990", EXIT_TIME, vehicle())

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None


def test_no_open_sessions_at_all(db):
    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert out.kind == ems.NO_CANDIDATES


# ── Bounds ───────────────────────────────────────────────────────────────────


def test_a_car_cannot_leave_before_it_arrived(db):
    open_session(db, "KXR-2538", hours_ago=-2.0)   # entered AFTER this exit

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert out.kind == ems.NO_CANDIDATES


def test_an_old_session_is_reachable_but_still_not_closed_on_a_string(db):
    """The 72h bound is gone, and what protects the phantom is no longer age.

    `EXIT_MATCH_MAX_AGE_HOURS=72` used to make a 200h session invisible to the
    matcher — which also made `ABR-8000` (98h) and `KBD-6795` (120h) unreachable
    by any exit, so the sessions that most needed resolving were exactly the ones
    hidden. They are candidates again.

    Being a candidate is not being closed. No string rule can close it; only slot
    evidence or appearance can, and both are statements about the physical car
    rather than about how long it has been sitting there.
    """
    open_session(db, "KXR-2538", hours_ago=200.0)

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None
    assert [c.plate for c in out.candidates] == ["KXR-2538"]


def test_the_feature_can_be_switched_off(db, monkeypatch):
    monkeypatch.setattr(settings, "EXIT_MATCH_ENABLED", False)
    open_session(db, "KXR-2538")

    out = ems.resolve_unmatched_exit(db, "AAB-2538", EXIT_TIME, vehicle())

    assert out.kind == ems.DISABLED
    assert out.session is None


# ── Appearance scoring: only where the plate rules gave up ───────────────────


def va_payload(*scores, quality_ok=True):
    return {
        "query_quality_ok": quality_ok,
        "query_sharpness": 180.0,
        "results": [{"plate": p, "score": s, "refs": 8} for p, s in scores],
    }


@pytest.mark.asyncio
async def test_appearance_breaks_a_plate_tie(db, monkeypatch):
    """Two identical digit groups — the plate rules refuse, appearance decides."""
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    async def fake(image_path, plates):
        assert set(plates) == {"KXR-2538", "ZZT-2538"}
        return va_payload(("KXR-2538", 0.91), ("ZZT-2538", 0.44))

    monkeypatch.setattr("app.utils.va_reid_client.compare", fake)

    out = await ems.resolve_with_appearance(
        db, "AAB-2538", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.matched
    assert out.session.plate_number == "KXR-2538"
    assert "appearance" in out.reason


@pytest.mark.asyncio
async def test_a_thin_margin_closes_nothing(db, monkeypatch):
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    async def fake(image_path, plates):
        return va_payload(("KXR-2538", 0.71), ("ZZT-2538", 0.68))

    monkeypatch.setattr("app.utils.va_reid_client.compare", fake)

    out = await ems.resolve_with_appearance(
        db, "AAB-2538", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None


@pytest.mark.asyncio
async def test_a_wide_margin_carries_a_modest_best_score(db, monkeypatch):
    """A clear winner matches even when its absolute score is unremarkable.

    These numbers are the real 2026-08-20 16:18 exit: the camera read
    KXR-2538's plate as "172538J", ReID put the correct car at 0.410 with a
    0.42 margin, and the old 0.50 absolute floor refused it. The stay stayed
    open for three days. The margin is the evidence; see
    EXIT_MATCH_REID_MIN_SCORE.
    """
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    async def fake(image_path, plates):
        return va_payload(("KXR-2538", 0.41), ("ZZT-2538", 0.02))

    monkeypatch.setattr("app.utils.va_reid_client.compare", fake)

    out = await ems.resolve_with_appearance(
        db, "172538J", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.matched
    assert out.session.plate_number == "KXR-2538"


@pytest.mark.asyncio
async def test_the_absolute_floor_still_applies_when_configured(db, monkeypatch):
    """The floor is opt-in, not deleted: setting it restores the old refusal."""
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    async def fake(image_path, plates):
        return va_payload(("KXR-2538", 0.41), ("ZZT-2538", 0.02))

    monkeypatch.setattr("app.utils.va_reid_client.compare", fake)
    monkeypatch.setattr(settings, "EXIT_MATCH_REID_MIN_SCORE", 0.50)

    out = await ems.resolve_with_appearance(
        db, "172538J", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None


@pytest.mark.asyncio
async def test_an_unusable_exit_crop_is_refused_not_scored(db, monkeypatch):
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    async def fake(image_path, plates):
        return va_payload(("KXR-2538", 0.99), ("ZZT-2538", 0.10), quality_ok=False)

    monkeypatch.setattr("app.utils.va_reid_client.compare", fake)

    out = await ems.resolve_with_appearance(
        db, "AAB-2538", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.kind == ems.AMBIGUOUS


@pytest.mark.asyncio
async def test_va_being_down_degrades_to_the_plate_answer(db, monkeypatch):
    open_session(db, "KXR-2538")
    open_session(db, "ZZT-2538")

    async def fake(image_path, plates):
        return None

    monkeypatch.setattr("app.utils.va_reid_client.compare", fake)

    out = await ems.resolve_with_appearance(
        db, "AAB-2538", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.kind == ems.AMBIGUOUS
    assert out.session is None


@pytest.mark.asyncio
async def test_appearance_never_second_guesses_a_trusted_plate(db, monkeypatch):
    """ENTRY_LOST is terminal — VA must not even be asked."""
    open_session(db, "KXR-2538")

    async def explode(image_path, plates):  # pragma: no cover
        raise AssertionError("VA must not be consulted for a trusted plate")

    monkeypatch.setattr("app.utils.va_reid_client.compare", explode)

    out = await ems.resolve_with_appearance(
        db, "AAB-2538", EXIT_TIME, vehicle(registered=True), "exit.jpg"
    )

    assert out.kind == ems.ENTRY_LOST


@pytest.mark.asyncio
async def test_appearance_is_the_only_route_to_a_close(db, monkeypatch):
    """Appearance is now the ONLY route from a candidate to a close.

    This test used to prove the opposite — that a unique digit match was already
    decisive and VA was not consulted. There are no plate rules left to be
    decisive, so VA IS consulted, and its answer is what closes the stay.
    """
    open_session(db, "KXR-2538")

    async def compare(image_path, plates):
        assert "KXR-2538" in plates
        return {
            "query_quality_ok": True,
            "results": [{"plate": "KXR-2538", "score": 0.91}],
        }

    monkeypatch.setattr("app.utils.va_reid_client.compare", compare)

    out = await ems.resolve_with_appearance(
        db, "AAB-2538", EXIT_TIME, vehicle(), "exit.jpg"
    )

    assert out.matched
    assert out.session.plate_number == "KXR-2538"
    assert out.reason.startswith("appearance:")
