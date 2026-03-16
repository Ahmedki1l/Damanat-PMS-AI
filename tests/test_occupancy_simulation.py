# tests/test_occupancy_simulation.py
"""
Simulation tests for the occupancy counting system.

These tests exercise the real service code against an in-memory SQLite DB
to verify fixes for known edge cases documented in the review report.
Each test logs every step with timestamps for post-mortem analysis.

Scenarios covered:
  1. Normal flow (enter → transition → exit)
  2. Double-fire deduplication (EVENT_STREAM_SUPPRESS_SECONDS window)
  3. Two-zone drift (savepoint rollback on exception between zone updates)
  4. Cache vs rollback mismatch (post-commit cache recording)
  5. Multi-worker cache isolation (known unfixed — requires Redis)
  6. Clamp-to-zero race condition (atomic func.max in SQL)
"""

import sys
import os
import time
import logging
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, event as sa_event
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database import Base
from app.models.zone_occupancy import ZoneOccupancy
from app.services.occupancy_service import (
    handle_occupancy_event,
    record_event_in_cache,
    push_db_update,
    _update_zone_count,
    _processed_events_cache,
    CACHE_TTL_SECONDS,
)
from app.services.event_parser import ParsedCameraEvent
from app.config import settings

# ── Logging setup ────────────────────────────────────────────────────────────

logger = logging.getLogger("test_occupancy_simulation")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(handler)


def ts():
    """Compact timestamp for inline log messages."""
    return datetime.utcnow().strftime("%H:%M:%S.%f")[:-3]


# ── Fixtures ─────────────────────────────────────────────────────────────────

# SQLite needs special config for SAVEPOINT support (used by FIX #1).
# The pysqlite driver auto-manages transactions by default, which breaks
# nested transactions. Setting isolation_level=None lets SQLAlchemy control it.
engine = create_engine("sqlite:///:memory:", echo=False)

@sa_event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """Enable proper SAVEPOINT support in SQLite."""
    dbapi_conn.isolation_level = None

@sa_event.listens_for(engine, "begin")
def _do_begin(conn):
    """Emit our own BEGIN so SQLAlchemy controls the transaction boundary."""
    conn.exec_driver_sql("BEGIN")

TestSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def db():
    """Create fresh tables, yield a session, then tear down."""
    Base.metadata.create_all(bind=engine)
    session = TestSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the in-memory dedup cache before every test."""
    _processed_events_cache.clear()
    yield
    _processed_events_cache.clear()


# ── Helpers ──────────────────────────────────────────────────────────────────

TOTAL = settings.GARAGE_TOTAL_ZONE   # "GARAGE-TOTAL"
B1    = settings.B1_PARKING_ZONE     # "B1-PARKING"
B2    = settings.B2_PARKING_ZONE     # "B2-PARKING"


def seed_zones(db, total=0, b1=0, b2=0, capacity=100):
    """Insert the three parking zones with given starting counts."""
    for zone_id, count, cam in [
        (TOTAL, total, "CAM-03"),
        (B1,    b1,    "CAM-03"),
        (B2,    b2,    "CAM-09"),
    ]:
        db.add(ZoneOccupancy(
            zone_id=zone_id,
            camera_id=cam,
            current_count=count,
            max_capacity=capacity,
            last_updated=datetime.utcnow(),
        ))
    db.commit()
    logger.info(f"[{ts()}] Seeded zones: {TOTAL}={total}, {B1}={b1}, {B2}={b2}")


def get_count(db, zone_id) -> int:
    """Read current_count for a zone, refreshing from DB first."""
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if zone:
        db.refresh(zone)
        return zone.current_count
    return -1  # zone missing


def log_all_zones(db, label=""):
    """Log the state of all three zones."""
    t = get_count(db, TOTAL)
    b1 = get_count(db, B1)
    b2 = get_count(db, B2)
    prefix = f"[{ts()}] {label} | " if label else f"[{ts()}] "
    logger.info(f"{prefix}{TOTAL}={t}, {B1}={b1}, {B2}={b2}")
    return t, b1, b2


def make_event(
    cam_id: str,
    region_id: str = "1",
    direction: str = None,
    event_type: str = "linedetection",
    target: str = "vehicle",
) -> ParsedCameraEvent:
    """Build a ParsedCameraEvent for testing."""
    return ParsedCameraEvent(
        camera_id=cam_id,
        device_serial="TEST-SN",
        channel_id=1,
        event_type=event_type,
        detection_target=target,
        region_id=region_id,
        channel_name=f"Test {cam_id}",
        trigger_time=datetime.utcnow(),
        raw_xml="<test/>",
        crossing_direction=direction,
    )


async def process_and_commit(event, db):
    """
    Simulate the full router flow: handle event → commit → record cache.
    This mirrors what events.py does after the fixes.
    """
    with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
        cache_key = await handle_occupancy_event(event, db)
    db.commit()
    # FIX #2: cache is recorded AFTER commit, just like the router does
    if cache_key:
        record_event_in_cache(cache_key)
    return cache_key


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 1: Normal flow — enter via CAM-03, transition B1→B2, exit via CAM-08
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario1_NormalFlow:

    @pytest.mark.asyncio
    async def test_full_vehicle_journey(self, db):
        """
        A vehicle enters the garage (CAM-03), moves from B1 to B2 (CAM-09),
        then exits the garage (CAM-08). Assert counts at every step.
        """
        seed_zones(db, total=5, b1=3, b2=2)

        # ── Step 1: Vehicle enters via CAM-03 (region_id=1 → forward → +1) ──
        logger.info(f"[{ts()}] STEP 1: Vehicle enters via CAM-03")
        event_enter = make_event("CAM-03", region_id="1")
        logger.info(f"[{ts()}] Event: cam=CAM-03, type=linedetection, region_id=1")

        await process_and_commit(event_enter, db)

        t, b1, b2 = log_all_zones(db, "After CAM-03 entry")
        assert t == 6, f"GARAGE-TOTAL should be 6, got {t}"
        assert b1 == 4, f"B1-PARKING should be 4, got {b1}"
        assert b2 == 2, f"B2-PARKING should be unchanged at 2, got {b2}"
        logger.info(f"[{ts()}] PASS Step 1: TOTAL=6, B1=4, B2=2")

        # ── Step 2: Vehicle transitions B1→B2 via CAM-09 (region_id=1 → forward) ─
        logger.info(f"[{ts()}] STEP 2: Vehicle transitions B1->B2 via CAM-09")
        event_transition = make_event("CAM-09", region_id="1")
        logger.info(f"[{ts()}] Event: cam=CAM-09, type=linedetection, region_id=1")

        await process_and_commit(event_transition, db)

        t, b1, b2 = log_all_zones(db, "After CAM-09 B1->B2")
        assert t == 6, f"GARAGE-TOTAL should still be 6 (transition doesn't affect total), got {t}"
        assert b1 == 3, f"B1-PARKING should be 3 (vehicle left B1), got {b1}"
        assert b2 == 3, f"B2-PARKING should be 3 (vehicle arrived B2), got {b2}"
        logger.info(f"[{ts()}] PASS Step 2: TOTAL=6, B1=3, B2=3")

        # ── Step 3: Vehicle exits via CAM-08 (region_id=1 → forward → -1 for exit cam) ─
        logger.info(f"[{ts()}] STEP 3: Vehicle exits via CAM-08")
        event_exit = make_event("CAM-08", region_id="1")
        logger.info(f"[{ts()}] Event: cam=CAM-08, type=linedetection, region_id=1")

        await process_and_commit(event_exit, db)

        t, b1, b2 = log_all_zones(db, "After CAM-08 exit")
        assert t == 5, f"GARAGE-TOTAL should be 5 (back to start), got {t}"
        assert b1 == 2, f"B1-PARKING should be 2 (CAM-08 decrements B1), got {b1}"
        assert b2 == 3, f"B2-PARKING should still be 3 (exit gate doesn't touch B2), got {b2}"
        logger.info(f"[{ts()}] PASS Step 3: TOTAL=5, B1=2, B2=3")

        # ── Invariant check ──
        final_t = get_count(db, TOTAL)
        final_b1 = get_count(db, B1)
        final_b2 = get_count(db, B2)
        logger.info(f"[{ts()}] INVARIANT CHECK: {TOTAL}={final_t} vs {B1}+{B2}={final_b1+final_b2}")
        assert final_t == final_b1 + final_b2, (
            f"DRIFT DETECTED: {TOTAL}={final_t} != {B1}({final_b1}) + {B2}({final_b2})"
        )
        logger.info(f"[{ts()}] PASS Invariant holds: TOTAL == B1 + B2")


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 2: Double-fire — camera sends duplicate events
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario2_DoubleFire:

    @pytest.mark.asyncio
    async def test_duplicate_within_window_is_dropped(self, db):
        """
        Same linedetection event sent twice within EVENT_STREAM_SUPPRESS_SECONDS.
        FIX #5: dedup window is now 30s (from config), not 1s.
        The second event should be dropped.
        """
        seed_zones(db, total=5, b1=5, b2=0)

        event1 = make_event("CAM-03", region_id="1")

        # ── Fire 1: Should be processed ──
        logger.info(f"[{ts()}] Fire 1: CAM-03 linedetection region_id=1")
        await process_and_commit(event1, db)

        t, _, _ = log_all_zones(db, "After fire 1")
        assert t == 6, f"First event should increment TOTAL to 6, got {t}"
        logger.info(f"[{ts()}] PASS Fire 1 counted: TOTAL=6")

        # ── Fire 2: Immediate retry (within suppress window) — should be DROPPED ──
        cache_key = ("CAM-03", "linedetection", "1")
        logger.info(f"[{ts()}] Fire 2: Duplicate within {CACHE_TTL_SECONDS}s window — expecting DROP")
        logger.info(f"[{ts()}] Cache state: key={cache_key} present={cache_key in _processed_events_cache}")

        event2 = make_event("CAM-03", region_id="1")
        await process_and_commit(event2, db)

        t, _, _ = log_all_zones(db, "After fire 2 (should be dropped)")
        assert t == 6, f"Duplicate within window should NOT increment. Expected 6, got {t}"
        logger.info(f"[{ts()}] PASS Fire 2 correctly dropped by cache: TOTAL still 6")

    @pytest.mark.asyncio
    async def test_duplicate_after_1s_still_blocked(self, db):
        """
        FIX #5 verification: same event sent 2s later is STILL blocked because
        the dedup window is now EVENT_STREAM_SUPPRESS_SECONDS (30s), not 1s.
        Previously this was the double-count bug — now it's fixed.
        """
        seed_zones(db, total=5, b1=5, b2=0)

        event1 = make_event("CAM-03", region_id="1")

        # ── Fire 1 ──
        logger.info(f"[{ts()}] Fire 1: CAM-03 linedetection region_id=1")
        await process_and_commit(event1, db)

        t_after_1, _, _ = log_all_zones(db, "After fire 1")
        assert t_after_1 == 6
        logger.info(f"[{ts()}] PASS Fire 1 counted: TOTAL=6")

        # ── Wait 2s (was long enough to bypass the old 1s TTL) ──
        wait_time = 2.0
        logger.info(f"[{ts()}] Waiting {wait_time}s (old TTL was 1s, new TTL is {CACHE_TTL_SECONDS}s)...")
        time.sleep(wait_time)
        logger.info(f"[{ts()}] Waited {wait_time}s — still within {CACHE_TTL_SECONDS}s window")

        # ── Fire 2: Should STILL be blocked (within 30s window) ──
        logger.info(f"[{ts()}] Fire 2: Same event after {wait_time}s — should be BLOCKED now")
        event2 = make_event("CAM-03", region_id="1")
        await process_and_commit(event2, db)

        t_after_2, _, _ = log_all_zones(db, "After fire 2")
        logger.info(
            f"[{ts()}] FIX VERIFIED: TOTAL stayed at {t_after_2}. "
            f"Old behavior would have double-counted to 7."
        )
        assert t_after_2 == 6, (
            f"FIX #5: duplicate after 2s should still be blocked (30s window). Got {t_after_2}"
        )


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 3: Two-zone drift — exception between zone updates
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario3_TwoZoneDrift:

    @pytest.mark.asyncio
    async def test_savepoint_prevents_drift_on_exception(self, db):
        """
        FIX #1 verification: simulate exception during the second _update_zone_count.
        The savepoint should roll back BOTH zone updates, preventing drift.

        Old behavior: GARAGE-TOTAL=11, B1=7 (drift of 1).
        New behavior: GARAGE-TOTAL=10, B1=7 (no drift — savepoint rolled back both).
        """
        seed_zones(db, total=10, b1=7, b2=3)
        log_all_zones(db, "Initial state")

        event = make_event("CAM-03", region_id="1")
        call_count = 0
        original_update = _update_zone_count

        async def patched_update_zone_count(zone_id, camera_id, delta, db_session):
            nonlocal call_count
            call_count += 1
            logger.info(f"[{ts()}] _update_zone_count call #{call_count}: zone={zone_id}, delta={delta}")

            if call_count == 1:
                # First call (GARAGE-TOTAL) — let it succeed
                await original_update(zone_id, camera_id, delta, db_session)
                logger.info(f"[{ts()}] PASS {zone_id} updated successfully")
            else:
                # Second call (B1-PARKING) — simulate crash
                logger.info(f"[{ts()}] SIMULATED EXCEPTION before updating {zone_id}")
                raise RuntimeError("Simulated crash between zone updates")

        logger.info(f"[{ts()}] Sending CAM-03 entry event with patched _update_zone_count")

        with patch("app.services.occupancy_service._update_zone_count", side_effect=patched_update_zone_count):
            with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
                # FIX #1: The savepoint catches the exception internally and rolls back.
                # handle_occupancy_event returns None (event not counted) instead of raising.
                cache_key = await handle_occupancy_event(event, db)

        db.commit()

        t, b1, b2 = log_all_zones(db, "After savepoint rollback")

        # FIX #1: BOTH zones should be unchanged — savepoint rolled back the first update too
        assert t == 10, f"GARAGE-TOTAL should still be 10 (savepoint rolled back), got {t}"
        assert b1 == 7, f"B1-PARKING should still be 7 (crash prevented update), got {b1}"
        assert t == b1 + b2, (
            f"FIX #1: No drift — TOTAL={t} == B1+B2={b1+b2}"
        )
        assert cache_key is None, "Event should not return a cache key after savepoint rollback"
        logger.info(f"[{ts()}] PASS FIX #1 verified: savepoint prevented drift. {TOTAL}=10, {B1}+{B2}=10")


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 4: Cache vs rollback mismatch
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario4_CacheRollbackMismatch:

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_rollback(self, db):
        """
        FIX #2 verification: event is processed, but DB transaction rolls back
        before cache is populated. Camera retry should succeed because cache
        is clean (no stale entry).

        Old behavior: cache blocked the retry → count stuck at 5.
        New behavior: retry succeeds → count correctly at 6.
        """
        seed_zones(db, total=5, b1=5, b2=0)
        log_all_zones(db, "Initial state")

        event = make_event("CAM-03", region_id="1")
        cache_key = ("CAM-03", "linedetection", "1")

        # ── Step 1: Process the event (zones flushed but NOT committed) ──
        logger.info(f"[{ts()}] Step 1: Processing event — flush succeeds, commit pending")
        with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
            returned_key = await handle_occupancy_event(event, db)

        # FIX #2: Cache should NOT be populated yet (it's only recorded after commit)
        logger.info(f"[{ts()}] Cache after processing: key={cache_key} present={cache_key in _processed_events_cache}")
        assert cache_key not in _processed_events_cache, (
            "FIX #2: Cache must NOT contain the key before commit"
        )
        t_after_flush, _, _ = log_all_zones(db, "After flush (pre-rollback)")
        assert t_after_flush == 6, f"Count should be 6 after flush, got {t_after_flush}"
        logger.info(f"[{ts()}] PASS Occupancy flushed to 6, cache is clean")

        # ── Step 2: Simulate DB error → rollback (e.g., CameraEvent insert fails) ──
        logger.info(f"[{ts()}] Step 2: SIMULATING DB ROLLBACK")
        db.rollback()
        # Do NOT call record_event_in_cache — simulating the router's error path

        t_after_rollback, _, _ = log_all_zones(db, "After rollback")
        assert t_after_rollback == 5, f"Rollback should restore count to 5, got {t_after_rollback}"
        assert cache_key not in _processed_events_cache, "Cache must still be empty after rollback"
        logger.info(f"[{ts()}] PASS DB rolled back to 5, cache still clean")

        # ── Step 3: Camera retries — should succeed because cache is clean ──
        logger.info(f"[{ts()}] Step 3: Camera retries — cache is clean, should process normally")
        event_retry = make_event("CAM-03", region_id="1")
        await process_and_commit(event_retry, db)

        t_after_retry, _, _ = log_all_zones(db, "After retry")

        # FIX #2: Retry succeeded — count is now correctly at 6
        logger.info(
            f"[{ts()}] FIX VERIFIED: Count is {t_after_retry}. "
            f"Old behavior would have stuck at 5 (cache blocked retry)."
        )
        assert t_after_retry == 6, (
            f"FIX #2: Retry after rollback should succeed. Expected 6, got {t_after_retry}"
        )
        assert cache_key in _processed_events_cache, "Cache should have the key now (after successful commit)"
        logger.info(f"[{ts()}] PASS FIX #2 verified: retry succeeded, count=6")


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 5: Multi-worker cache isolation (KNOWN UNFIXED — requires Redis)
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario5_MultiWorkerIsolation:

    @pytest.mark.asyncio
    async def test_separate_caches_allow_double_counting(self, db):
        """
        Known unfixed issue: two worker processes each with their own dedup cache.
        The same event reaches both workers → both process it → double count.
        Fix requires shared cache (Redis) — out of scope for now.

        We simulate this by clearing the cache between invocations (simulating
        a second worker's empty cache) while keeping the same DB session.
        """
        seed_zones(db, total=5, b1=5, b2=0)
        log_all_zones(db, "Initial state")

        event = make_event("CAM-03", region_id="1")
        cache_key = ("CAM-03", "linedetection", "1")

        # ── Worker 1 processes the event ──
        logger.info(f"[{ts()}] Worker 1: Processing CAM-03 entry event")
        await process_and_commit(event, db)

        t_w1, _, _ = log_all_zones(db, "After Worker 1")
        assert t_w1 == 6
        logger.info(f"[{ts()}] PASS Worker 1 processed: TOTAL=6")
        logger.info(f"[{ts()}] Worker 1 cache: {cache_key} present={cache_key in _processed_events_cache}")

        # ── Worker 2 has its own empty cache (simulated by clearing) ──
        logger.info(f"[{ts()}] Simulating Worker 2: clearing cache (separate process memory)")
        _processed_events_cache.clear()
        logger.info(f"[{ts()}] Worker 2 cache: {cache_key} present={cache_key in _processed_events_cache}")

        # ── Worker 2 processes the SAME event ──
        logger.info(f"[{ts()}] Worker 2: Processing same CAM-03 entry event")
        event_w2 = make_event("CAM-03", region_id="1")
        await process_and_commit(event_w2, db)

        t_w2, _, _ = log_all_zones(db, "After Worker 2")

        logger.info(
            f"[{ts()}] KNOWN BUG (unfixed): Same event counted by both workers. "
            f"TOTAL went 5->6->{t_w2}. Requires Redis for shared dedup."
        )
        assert t_w2 == 7, (
            f"Expected double-count to 7 (no shared cache). Got {t_w2}"
        )
        logger.info(f"[{ts()}] PASS Known bug confirmed: multi-worker double-count, TOTAL=7")


# ═════════════════════════════════════════════════════════════════════════════
# SCENARIO 6: Clamp-to-zero race condition
# ═════════════════════════════════════════════════════════════════════════════

class TestScenario6_ClampToZeroRace:

    @pytest.mark.asyncio
    async def test_count_never_goes_negative(self, db):
        """
        Baseline: a single exit event when count=1 should result in 0, not -1.
        """
        seed_zones(db, total=1, b1=1, b2=0)
        log_all_zones(db, "Initial state (count=1)")

        event = make_event("CAM-08", region_id="1")  # exit → delta = -1
        logger.info(f"[{ts()}] Sending exit event via CAM-08 with count=1")

        await process_and_commit(event, db)

        t, b1, _ = log_all_zones(db, "After exit (should be 0)")
        assert t == 0, f"TOTAL should be 0, got {t}"
        assert b1 == 0, f"B1 should be 0, got {b1}"
        assert t >= 0, "Count must never be negative"
        logger.info(f"[{ts()}] PASS Count correctly at 0")

    @pytest.mark.asyncio
    async def test_double_exit_clamps_to_zero(self, db):
        """
        Two exit events when count=1. First brings it to 0, second should clamp to 0.
        """
        seed_zones(db, total=1, b1=1, b2=0)
        log_all_zones(db, "Initial state (count=1)")

        # ── Exit 1 ──
        event1 = make_event("CAM-08", region_id="1")
        logger.info(f"[{ts()}] Exit 1: CAM-08 with count=1")
        await process_and_commit(event1, db)

        t, _, _ = log_all_zones(db, "After exit 1")
        assert t == 0
        logger.info(f"[{ts()}] PASS Exit 1: TOTAL=0")

        # Clear cache so second event isn't deduped
        _processed_events_cache.clear()

        # ── Exit 2: count is already 0 ──
        event2 = make_event("CAM-08", region_id="1")
        logger.info(f"[{ts()}] Exit 2: CAM-08 with count=0 (should clamp)")
        await process_and_commit(event2, db)

        t, b1, _ = log_all_zones(db, "After exit 2 (should still be 0)")
        assert t == 0, f"TOTAL should clamp to 0, got {t}"
        assert b1 == 0, f"B1 should clamp to 0, got {b1}"
        logger.info(f"[{ts()}] PASS Clamp confirmed: TOTAL=0 after second exit")

    @pytest.mark.asyncio
    async def test_concurrent_exits_atomic_clamp(self, db):
        """
        FIX #3 verification: two exit events when count=1.
        With atomic func.max(count + delta, 0), the SQL level never goes
        negative — eliminating the race window entirely.

        Old behavior: push_db_update drove count to -1, then ORM clamp fixed it.
        New behavior: SQL max() ensures count is always >= 0 at the DB level.
        """
        seed_zones(db, total=1, b1=1, b2=0)
        log_all_zones(db, "Initial state (count=1)")

        logger.info(f"[{ts()}] Simulating concurrent exit race with count=1")

        # Track what push_db_update does at the SQL level
        updates_applied = []
        original_push = push_db_update

        async def tracking_push(db_session, zone_id, delta):
            logger.info(f"[{ts()}] push_db_update: zone={zone_id}, delta={delta}")
            await original_push(db_session, zone_id, delta)
            db_session.flush()
            zone = db_session.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
            if zone:
                db_session.refresh(zone)
                logger.info(f"[{ts()}] After push: {zone_id}={zone.current_count}")
                updates_applied.append((zone_id, delta, zone.current_count))

        # ── Exit 1 ──
        event1 = make_event("CAM-08", region_id="1")
        logger.info(f"[{ts()}] Exit 1: Processing")
        with patch("app.services.occupancy_service.push_db_update", side_effect=tracking_push):
            with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
                await handle_occupancy_event(event1, db)
        db.commit()
        record_event_in_cache(("CAM-08", "linedetection", "1"))

        t1, b1_1, _ = log_all_zones(db, "After exit 1")
        logger.info(f"[{ts()}] Exit 1 result: TOTAL={t1}, B1={b1_1}")

        # Clear cache for second concurrent exit
        _processed_events_cache.clear()

        # ── Exit 2 (concurrent — count is already 0) ──
        event2 = make_event("CAM-08", region_id="1")
        logger.info(f"[{ts()}] Exit 2: Processing (count is already 0)")
        with patch("app.services.occupancy_service.push_db_update", side_effect=tracking_push):
            with patch("app.services.occupancy_service.create_alert", new_callable=AsyncMock):
                await handle_occupancy_event(event2, db)
        db.commit()

        t2, b1_2, _ = log_all_zones(db, "After exit 2")

        logger.info(f"[{ts()}] Updates applied: {updates_applied}")
        logger.info(
            f"[{ts()}] RACE RESULT: TOTAL went 1->{t1}->{t2}. "
            f"Checking SQL-level counts never went negative."
        )

        # FIX #3: The atomic func.max() means the SQL count NEVER goes negative
        assert t2 >= 0, f"Count must never go negative, got {t2}"
        assert b1_2 >= 0, f"B1 count must never go negative, got {b1_2}"

        negative_pushes = [(z, d, c) for z, d, c in updates_applied if c < 0]
        assert len(negative_pushes) == 0, (
            f"FIX #3: SQL-level count should never go negative. "
            f"Negative values found: {negative_pushes}"
        )
        logger.info(
            f"[{ts()}] PASS FIX #3 verified: atomic clamp held. "
            f"No negative SQL counts. TOTAL={t2}, B1={b1_2}"
        )
