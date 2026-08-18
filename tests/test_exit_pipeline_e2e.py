"""End-to-end: the whole exit pipeline, Stages 1-4 integrated.

    ingest (edge | Hik sweep)
      -> HikCentral plate correction
      -> exact match on the unbounded pool
      -> slot evidence
      -> Re-ID
      -> close + plate rewrite  |  Log X

Numbered to the integrated scenario document. These drive the real entry points —
`handle_anpr_event` and `_reconcile_missed_exits` — against a real database, with
only the two genuine outside edges stubbed: HikCentral's HTTP client and VA's.

The tiers are ordered by how much they can cost when wrong, and several tests
here exist to prove a tier was NOT consulted: an exact match must not reach the
slot filter, and a unique slot confirmation must not reach Re-ID. Cheapness is
not the reason — each extra tier is another way to answer a question that was
already settled.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models.entry_exit_log import EntryExitLog
from app.models.hik_validation import HikValidation
from app.models.parking_session import ParkingSession
from app.models.vehicle import Vehicle
from app.services import (
    entry_exit_service,
    exit_match_service,
    exit_pipeline,
    hikcentral,
    parking_session_service as pss,
)
from app.services.event_parser import ParsedCameraEvent
from app.services.hikcentral.models import HikImages, VehicleLogRecord

FTZ = timezone(timedelta(hours=3))
ENTRY = datetime(2026, 8, 16, 7, 0, 0)
EXIT = datetime(2026, 8, 16, 9, 30, 0)

engine = create_engine("sqlite:///:memory:")
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    # VA-owned tables. PMS-AI has no model for them by design, so the pipeline
    # reads them with raw SQL and the tests create them the same way.
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS parking_slots (slot_id VARCHAR(50) PRIMARY "
        "KEY, is_available BOOLEAN, current_plate VARCHAR(50))"
    ))
    session.execute(text(
        "CREATE TABLE IF NOT EXISTS slot_status (id INTEGER PRIMARY KEY, "
        "slot_id VARCHAR(50), plate_number VARCHAR(20), status VARCHAR(20), "
        "time DATETIME)"
    ))
    session.commit()
    try:
        yield session
    finally:
        session.rollback()
        session.execute(text("DELETE FROM parking_slots"))
        session.execute(text("DELETE FROM slot_status"))
        session.commit()
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def pipeline(monkeypatch):
    monkeypatch.setattr(settings, "HIK_VALIDATION_MODE", "authoritative")
    monkeypatch.setattr(settings, "HIK_EXIT_RESOURCE_IDS", "510")
    monkeypatch.setattr(settings, "HIK_RECONCILE_MATCH_SECONDS", 30.0)
    monkeypatch.setattr(settings, "EXIT_HIK_RECHECK_SECONDS", 0)
    monkeypatch.setattr(settings, "EXIT_SLOT_EVIDENCE_ENABLED", True)
    monkeypatch.setattr(settings, "EXIT_DRIVE_OUT_SECONDS", 120.0)
    monkeypatch.setattr(settings, "EXIT_MATCH_REID_ENABLED", True)
    monkeypatch.setattr(
        settings, "CAMERAS",
        {"CAM-ENTRY": {"gate": "entry"}, "CAM-EXIT": {"gate": "exit"}},
    )
    monkeypatch.setattr(entry_exit_service, "facility_now_naive", lambda: EXIT)
    monkeypatch.setattr(
        "app.utils.core_backend_client.notify_pms_anpr", AsyncMock()
    )


# ── stubs for the two real outside edges ────────────────────────────────────


def hik_holds(monkeypatch, *plates, at=EXIT):
    """HikCentral's answer for the exit camera."""
    records = [
        VehicleLogRecord.from_openapi_record({
            "crossRecordSyscode": f"G-{i}", "cameraIndexCode": "510",
            "plateNo": p, "crossTime": at.replace(tzinfo=FTZ).isoformat(),
            "vehiclePicUri": "Vsm://v",
        })
        for i, p in enumerate(plates)
    ]

    async def _query(**kwargs):
        return list(records)

    monkeypatch.setattr(
        "app.services.hikcentral.client.query_vehicle_logs", _query
    )


def hik_unreachable(monkeypatch):
    async def _down(path, body_obj):
        return None      # what the client returns for a timeout or a 5xx

    monkeypatch.setattr("app.services.hikcentral.client._signed_post", _down)


def reid_scores(monkeypatch, **scores):
    calls = []

    async def compare(image_path, plates):
        calls.append(list(plates))
        return {"query_quality_ok": True, "results": [
            {"plate": p, "score": s} for p, s in scores.items() if p in plates
        ]}

    monkeypatch.setattr("app.utils.va_reid_client.compare", compare)
    return calls


def reid_must_not_run(monkeypatch):
    async def explode(image_path, plates):  # pragma: no cover
        raise AssertionError(f"Re-ID must not be consulted (asked {plates})")

    monkeypatch.setattr("app.utils.va_reid_client.compare", explode)


def va_rename(monkeypatch, ok=True):
    sent = []

    async def rename(old, new):
        sent.append((old, new))
        if not ok:
            return False
        return True

    monkeypatch.setattr("app.utils.va_reid_client.rename", rename)
    return sent


def va_rename_raises(monkeypatch):
    """A VA that refuses the connection rather than answering politely."""
    async def rename(old, new):
        raise ConnectionRefusedError("VA is down")

    monkeypatch.setattr("app.utils.va_reid_client.rename", rename)


# ── fixtures for the world ──────────────────────────────────────────────────


def stay(db, plate, slot_id=None, entry_time=ENTRY):
    row = ParkingSession(
        plate_number=plate, entry_time=entry_time, entry_camera_id="CAM-ENTRY",
        status="open", slot_id=slot_id, created_at=entry_time,
        updated_at=entry_time,
    )
    db.add(row)
    db.add(EntryExitLog(
        plate_number=plate, gate="entry", event_time=entry_time,
        camera_id="CAM-ENTRY",
    ))
    db.flush()
    return row


def slot(db, slot_id, occupied, plate=None):
    db.execute(
        text("INSERT INTO parking_slots (slot_id, is_available, current_plate) "
             "VALUES (:s, :a, :p)"),
        {"s": slot_id, "a": not occupied, "p": plate},
    )
    db.flush()


def vacated(db, slot_id, when, plate=None):
    db.execute(
        text("INSERT INTO slot_status (slot_id, plate_number, status, time) "
             "VALUES (:s, :p, 'available', :t)"),
        {"s": slot_id, "p": plate, "t": when},
    )
    db.flush()


def edge_exit(plate, at=EXIT):
    return ParsedCameraEvent(
        camera_id="CAM-EXIT", device_serial="S", channel_id=1,
        event_type="AccessControllerEvent", detection_target="vehicle",
        region_id="exit", channel_name="Exit", trigger_time=at.replace(tzinfo=FTZ),
        raw_xml="{}", plate_number=plate, gate="exit",
        snapshot_path="/snap/exit.jpg", local_snapshot_path="/snap/exit.jpg",
    )


async def sweep(db, monkeypatch, plate_no, guid="G-SWEEP", at=EXIT, images=None):
    async def fake_list(resource_ids, begin, end, db):
        return [VehicleLogRecord.from_openapi_record({
            "crossRecordSyscode": guid, "cameraIndexCode": "510",
            "plateNo": plate_no, "crossTime": at.replace(tzinfo=FTZ).isoformat(),
            "vehiclePicUri": "Vsm://v",
        })]

    async def fake_images(outcome):
        return images or HikImages()

    monkeypatch.setattr(hikcentral, "list_unconsumed_records", fake_list)
    monkeypatch.setattr(hikcentral, "download_hik_images", fake_images)
    await entry_exit_service._reconcile_missed_exits(
        db, window=(at - timedelta(hours=1), at + timedelta(minutes=5))
    )


def exit_rows(db):
    return db.query(EntryExitLog).filter(EntryExitLog.gate == "exit").all()


# ══════════════════════════════════════════════════════════════════════════
# 1. Happy path and ingest topology
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_1_1_exact_match_via_live_edge_webhook(db, monkeypatch):
    """The ~130-a-week path. It must stop at the exact tier.

    The assertion that matters is the negative one: neither slot evidence nor
    Re-ID is consulted. A settled question must not be re-asked by a tier that
    could answer it differently.

    (There is no fee computation anywhere in PMS-AI — `grep` for
    fee/tariff/billing finds nothing. It computes duration; pricing is another
    service's job.)
    """
    target = stay(db, "SDD-6707", slot_id="B6")
    slot(db, "B6", occupied=True, plate="SDD-6707")   # would ELIMINATE if asked
    hik_holds(monkeypatch, "6707SDD")
    reid_must_not_run(monkeypatch)
    monkeypatch.setattr(
        exit_match_service, "slot_evidence",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("slot evidence must not run after an exact match")
        ),
    )

    await entry_exit_service.handle_anpr_event(edge_exit("SDD-6707"), db)
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    assert target.duration_seconds == 9000
    row = exit_rows(db)[0]
    assert row.plate_number == "SDD-6707"
    assert row.parking_duration == 9000
    assert row.matched_entry_id is not None


@pytest.mark.asyncio
async def test_1_2_exact_match_via_reconcile_sweep(db, monkeypatch):
    """The live webhook was dropped. The sweep must reach the same outcome, and
    consume the GUID so the pass cannot be processed twice."""
    target = stay(db, "SDD-6707")

    await sweep(db, monkeypatch, "6707SDD", guid="G-DROP")
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    assert target.duration_seconds == 9000
    assert target.exit_camera_id == exit_pipeline.RECONCILE_CAMERA_ID
    ledger = db.query(HikValidation).filter(HikValidation.guid == "G-DROP").one()
    assert ledger.direction == hikcentral.DIRECTION_EXIT
    assert len(exit_rows(db)) == 1


@pytest.mark.asyncio
async def test_1_2b_a_late_webhook_after_the_sweep_closes_nothing_twice(
    db, monkeypatch
):
    """The dropped webhook arrives after the sweep already handled the pass.
    One car leaving must still be one closure and one audit row."""
    target = stay(db, "SDD-6707")
    await sweep(db, monkeypatch, "6707SDD", guid="G-DROP")
    db.commit()
    first_exit_time = target.exit_time

    hik_holds(monkeypatch, "6707SDD")
    await entry_exit_service.handle_anpr_event(edge_exit("SDD-6707"), db)
    db.commit()

    db.refresh(target)
    assert target.exit_time == first_exit_time, "the first closure must stand"
    assert len(exit_rows(db)) == 1, "the late webhook must not add a second row"


@pytest.mark.asyncio
async def test_1_3_dash_and_order_normalization_end_to_end(db, monkeypatch):
    """`normalize_plate` passes a dashed read through unchanged, so string
    equality saw `6707-SDD` and `SDD-6707` as two cars. It resolves on the exact
    tier — it must not be rescued by a lower one."""
    target = stay(db, "SDD-6707")
    hik_holds(monkeypatch)          # platform has nothing; the edge plate stands
    reid_must_not_run(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("6707-SDD"), db)
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    assert target.plate_number == "SDD-6707", "no correction was needed"


# ══════════════════════════════════════════════════════════════════════════
# 2. HikCentral corrects the exit plate at the source (Stage 1)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_2_1_a_camera_misread_is_corrected_before_matching(db, monkeypatch):
    """`AAA-2538` on 8/11 and 8/12 was the same car as `KXR-2538`. Corrected at
    the source, the exact tier simply works — and the raw camera read survives in
    the ledger, which is the only place it is allowed to survive."""
    target = stay(db, "KXR-2538")
    hik_holds(monkeypatch, "2538KXR")
    reid_must_not_run(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("AAA-2538"), db)
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    row = exit_rows(db)[0]
    assert row.plate_number == "KXR-2538", "the audit row uses the real plate"

    ledger = db.query(HikValidation).filter(
        HikValidation.direction == hikcentral.DIRECTION_EXIT
    ).one()
    assert ledger.reported_plate == "AAA-2538", "the misread must be recoverable"
    assert ledger.canonical_plate == "KXR-2538"
    assert ledger.entry_exit_log_id == row.id


@pytest.mark.asyncio
async def test_2_2_an_unreachable_platform_degrades_to_the_edge_plate(
    db, monkeypatch
):
    """HikCentral is a second opinion. One that does not arrive changes nothing,
    and must not block or crash the gate."""
    target = stay(db, "EGY-9999")
    hik_unreachable(monkeypatch)
    reid_must_not_run(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("EGY-9999"), db)
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    assert exit_rows(db)[0].plate_number == "EGY-9999"
    assert db.query(HikValidation).count() == 0, "nothing to record"


# ══════════════════════════════════════════════════════════════════════════
# 3. The entry misread is fixed at the exit (Stage 2)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_3_1_an_entry_misread_is_rewritten_from_the_exit(db, monkeypatch):
    """The whole point of the pipeline. Only the exit can correct an entry: every
    entry burst in the 8/10-8/16 window had reads=1 and HikCentral is fed by the
    same entry LPR, so nothing upstream can catch a wrong entry plate."""
    target = stay(db, "ABC-123", slot_id="B7")
    slot(db, "B7", occupied=False)
    vacated(db, "B7", EXIT - timedelta(seconds=60))
    db.add(Vehicle(
        plate_number="ABC-123", owner_name="Unknown", title="Unknown",
        vehicle_type="unknown", is_registered=False,
    ))
    db.flush()
    hik_holds(monkeypatch)
    sent = va_rename(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("ABC-1234"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    db.refresh(target)
    assert target.status == "closed"
    assert target.plate_number == "ABC-1234", "the stay carries the real plate"

    ledger = db.query(HikValidation).filter(
        HikValidation.session_id == target.id
    ).one()
    assert ledger.reported_plate == "ABC-123"
    assert ledger.canonical_plate == "ABC-1234"

    entry_row = db.query(EntryExitLog).filter(EntryExitLog.gate == "entry").one()
    assert entry_row.plate_number == "ABC-1234", "the entry row follows the stay"
    assert db.query(Vehicle).filter(
        Vehicle.plate_number == "ABC-1234"
    ).count() == 1
    assert sent == [("ABC-123", "ABC-1234")], "VA is told, after the commit"


@pytest.mark.asyncio
async def test_3_2_a_dead_va_does_not_roll_back_the_correction(db, monkeypatch):
    """`apply_correction` has already committed by the time VA is told. A VA that
    refuses the connection must not be able to undo it."""
    target = stay(db, "ABC-123", slot_id="B7")
    slot(db, "B7", occupied=False)
    vacated(db, "B7", EXIT - timedelta(seconds=60))
    hik_holds(monkeypatch)
    va_rename_raises(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("ABC-1234"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    db.refresh(target)
    assert target.status == "closed"
    assert target.plate_number == "ABC-1234"
    assert db.query(HikValidation).filter(
        HikValidation.session_id == target.id
    ).count() == 1


# ══════════════════════════════════════════════════════════════════════════
# 4. Slot evidence (Stage 4)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_4_1_a_unique_vacancy_closes_without_reid(db, monkeypatch):
    """The strongest evidence in the system. One slot emptied while one car left;
    Re-ID has nothing to add and is not asked."""
    target = stay(db, "SDD-6707", slot_id="B6_Reserved")
    slot(db, "B6_Reserved", occupied=False)
    vacated(db, "B6_Reserved", EXIT - timedelta(seconds=45))
    hik_holds(monkeypatch)
    reid_must_not_run(monkeypatch)
    va_rename(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("XYZ-0000"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    db.refresh(target)
    assert target.status == "closed"
    assert target.plate_number == "XYZ-0000", "closed AND corrected"


@pytest.mark.asyncio
async def test_4_2_a_still_parked_candidate_never_reaches_reid(db, monkeypatch):
    """The ordering exists for this. Appearance is never asked to choose between
    a car that left and a car VA is watching sit in its slot."""
    parked = stay(db, "CAR-1", slot_id="A1")
    gone = stay(db, "CAR-2", slot_id="A2")
    slot(db, "A1", occupied=True, plate="CAR-1")
    slot(db, "A2", occupied=False)
    hik_holds(monkeypatch)
    asked = reid_scores(monkeypatch, **{"CAR-2": 0.9})
    va_rename(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("UNKNOWN-9"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    assert asked == [["CAR-2"]], "the parked car is protected from appearance"
    db.refresh(parked)
    db.refresh(gone)
    assert parked.status == "open"
    assert gone.status == "closed"


@pytest.mark.asyncio
async def test_4_3_a_b2_car_with_no_slot_signal_survives_to_reid(db, monkeypatch):
    """15 of 35 slots run VA_IDENTITY_DISABLED and produce no plate signal. No
    evidence is not evidence against — treating it as elimination would make
    every B2 car permanently unmatchable."""
    target = stay(db, "B2CAR-1", slot_id=None)
    hik_holds(monkeypatch)
    asked = reid_scores(monkeypatch, **{"B2CAR-1": 0.88})
    va_rename(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("UNKNOWN-8"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    assert asked == [["B2CAR-1"]]
    db.refresh(target)
    assert target.status == "closed"


# ══════════════════════════════════════════════════════════════════════════
# 5. Re-ID, and the exits nothing can place
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_5_1_a_confident_reid_answer_closes_and_corrects(db, monkeypatch):
    """0.82 against 0.41 is a margin of 0.41, above the 0.35 gate — the measured
    100%-precision point on the real gallery."""
    winner = stay(db, "CAND-A", slot_id=None)
    loser = stay(db, "CAND-B", slot_id=None)
    hik_holds(monkeypatch)
    reid_scores(monkeypatch, **{"CAND-A": 0.82, "CAND-B": 0.41})
    sent = va_rename(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("UNKNOWN-1"), db)
    db.commit()
    await exit_pipeline.drain_late_rechecks()

    db.refresh(winner)
    db.refresh(loser)
    assert winner.status == "closed"
    assert winner.plate_number == "UNKNOWN-1"
    assert loser.status == "open"
    assert sent == [("CAND-A", "UNKNOWN-1")]


@pytest.mark.asyncio
async def test_5_2_an_ambiguous_reid_answer_logs_x_and_closes_nothing(
    db, monkeypatch, caplog
):
    """0.52 against 0.48 is a margin of 0.04. There is no string rule left to
    fall back on, so the pipeline refuses — and the refusal must be legible.

    Log X is the deliverable here: without the candidates, their metrics and
    their slot verdicts on one line, "unresolved" cannot be told apart from
    "never looked", and an operator meets an unexplained open stay days later.
    """
    a = stay(db, "CAND-A", slot_id=None)
    b = stay(db, "CAND-B", slot_id=None)
    hik_holds(monkeypatch)
    reid_scores(monkeypatch, **{"CAND-A": 0.52, "CAND-B": 0.48})

    with caplog.at_level("WARNING"):
        await entry_exit_service.handle_anpr_event(edge_exit("UNKNOWN-2"), db)
    db.commit()

    db.refresh(a)
    db.refresh(b)
    assert a.status == b.status == "open", "nothing may close on a 0.04 margin"
    assert db.query(HikValidation).count() == 0, "and nothing may be rewritten"

    log_x = [r.message for r in caplog.records if "Exit X" in r.message]
    assert log_x, "an unresolved exit must leave a Log X line"
    line = log_x[0]
    assert "UNKNOWN-2" in line and "CAND-A" in line and "CAND-B" in line
    assert "slot=" in line, "the slot verdict belongs in the record"
    assert "0.5" in line or "margin" in line, "so does the appearance margin"

    row = exit_rows(db)[0]
    assert row.plate_number == "UNKNOWN-2"
    assert row.matched_entry_id is None, "written unmatched, not forced"


# ══════════════════════════════════════════════════════════════════════════
# 6. History and concurrency
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_6_1_a_120h_stay_still_resolves(db, monkeypatch):
    """`KBD-6795` at 120h and `ABR-8000` at 98h were invisible to the matcher
    under the old 72h bound — the stays that most needed resolving were the ones
    it hid."""
    target = stay(db, "KBD-6795", entry_time=EXIT - timedelta(hours=120))
    hik_holds(monkeypatch)

    await entry_exit_service.handle_anpr_event(edge_exit("KBD-6795"), db)
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    assert target.duration_seconds == 120 * 3600


def test_6_2_the_webhook_and_the_sweep_cannot_both_close_one_stay(tmp_path):
    """Both ingest paths run the full pipeline since Stage 1, so both can reach
    one stay. Two real transactions, both reading it as open before either
    writes — the guard is the UPDATE's own `WHERE status = 'open'`, so the
    database decides and the loser gets a clean refusal.
    """
    url = f"sqlite:///{tmp_path / 'race.db'}"
    race_engine = create_engine(url)
    Base.metadata.create_all(bind=race_engine)
    Maker = sessionmaker(autocommit=False, autoflush=False, bind=race_engine)

    seed = Maker()
    stay(seed, "RACE-1")
    seed.commit()
    seed.close()

    webhook, sweep_worker = Maker(), Maker()
    try:
        stale = pss.open_stays(sweep_worker, EXIT)[0]
        assert pss.open_stays(webhook, EXIT), "both see it open first"

        won = pss.close_session(
            webhook, plate_number="RACE-1", event_time=EXIT,
            camera_id="CAM-EXIT", snapshot_path=None,
        )
        webhook.commit()

        lost = pss.close_matched_session(
            sweep_worker, stale, exit_time=EXIT + timedelta(hours=1),
            camera_id=exit_pipeline.RECONCILE_CAMERA_ID, snapshot_path=None,
        )
        sweep_worker.commit()

        assert won is not None and lost is None

        check = Maker()
        final = check.query(ParkingSession).one()
        assert final.status == "closed"
        assert final.exit_time == EXIT
        assert final.exit_camera_id == "CAM-EXIT", "the winner's close stands"
        check.close()
    finally:
        webhook.close()
        sweep_worker.close()
        Base.metadata.drop_all(bind=race_engine)


@pytest.mark.asyncio
async def test_3_2b_a_raising_va_does_not_abandon_the_reconcile_sweep(
    db, monkeypatch
):
    """The sweep AWAITS `notify_va` directly after its commit, so an exception
    escaping it would abandon the rest of a catch-up chunk — one VA hiccup
    costing a whole sweep, long after the correction it was reporting is durable.
    """
    target = stay(db, "ABC-123", slot_id="B7")
    slot(db, "B7", occupied=False)
    vacated(db, "B7", EXIT - timedelta(seconds=60))
    va_rename_raises(monkeypatch)

    await sweep(db, monkeypatch, "1234ABC", guid="G-VA",
                images=HikImages(vehicle_image_path="/snap/rec.jpg"))
    db.commit()

    db.refresh(target)
    assert target.status == "closed"
    assert target.plate_number == "ABC-1234", "the correction stands"
    assert db.query(HikValidation).filter(
        HikValidation.guid == "G-VA"
    ).count() == 1, "the sweep still consumed its GUID"
