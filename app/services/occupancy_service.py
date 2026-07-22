# app/services/occupancy_service.py
from typing import Optional
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from app.models.zone_occupancy import ZoneOccupancy
from app.models.parking_session import ParkingSession
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings, facility_now_naive
from app.utils.logger import get_logger

logger = get_logger(__name__)

# In-memory cache to prevent double-counting from identical events.
# FIX #2 (cache-vs-rollback): entries are only added AFTER a successful DB commit,
# not before — so a rollback won't leave stale keys that block camera retries.
# FIX #5 (double-fire >1s): TTL uses EVENT_STREAM_SUPPRESS_SECONDS (default 30s)
# instead of a hardcoded 1s, matching the suppression window used by other services.
# Format: {(camera_id, event_type, plate_or_region): timestamp}
_processed_events_cache = {}
CACHE_TTL_SECONDS = settings.EVENT_STREAM_SUPPRESS_SECONDS

# Cooldown cache for capacity_exceeded alerts: prevents firing the same alert
# repeatedly while a zone stays full. Key: zone_id, Value: last alert timestamp.
_capacity_alert_cache: dict[str, float] = {}
CAPACITY_ALERT_COOLDOWN = settings.CAPACITY_ALERT_COOLDOWN_SECONDS


async def _real_capacity_state(db: Session, zone_id: str, floor: Optional[str]) -> tuple[int, int]:
    """Return `(real_max_capacity, real_occupancy)` for a zone, sourced from
    the same tables the gateway dashboard reads — so the alert text agrees
    with what operators see on the cards.

    `real_max_capacity` counts `parking_slots` rows (monitored + unmonitored)
    excluding violation-zone slots — the true floor size, not the line-crossing
    aggregate which can drift.

    `real_occupancy` is the count of open `parking_sessions` rows — physical
    cars in the garage right now (not the line-crossing tick count).

    Scope:
      - `GARAGE_TOTAL_ZONE` or unknown floor → garage-wide counts.
      - per-floor zone with a configured `floor` → floor-scoped counts.

    Defensive: the `parking_slots` table isn't owned by PMS-AI's Alembic
    migrations (the gateway provisions it via `sql/bootstrap.sql`), so on
    test rigs or fresh deployments where the table doesn't exist yet, the
    query 500s. We catch + return (0, occ) so the trigger short-circuits
    (no alert) rather than aborting the whole occupancy update.
    """
    try:
        if zone_id == settings.GARAGE_TOTAL_ZONE or not floor:
            real_max = db.execute(
                text("SELECT COUNT(*) FROM parking_slots WHERE is_violation_zone = 0")
            ).scalar() or 0
        else:
            real_max = db.execute(
                text("SELECT COUNT(*) FROM parking_slots WHERE floor = :f AND is_violation_zone = 0"),
                {"f": floor},
            ).scalar() or 0
    except Exception as e:
        logger.debug(f"[UC3] parking_slots unavailable for {zone_id}: {e}; treating real_max as 0")
        real_max = 0

    sessions_q = db.query(func.count(ParkingSession.id)).filter(ParkingSession.status == "open")
    if zone_id != settings.GARAGE_TOTAL_ZONE and floor:
        sessions_q = sessions_q.filter(ParkingSession.floor == floor)
    real_occupancy = sessions_q.scalar() or 0

    return int(real_max), int(real_occupancy)


def _ensure_zone(db: Session, zone_id: str, camera_id: str) -> ZoneOccupancy:
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    zone_meta = settings.get_zone_metadata(zone_id)
    if not zone:
        logger.info(f"[UC3] Auto-creating zone '{zone_id}'")
        zone = ZoneOccupancy(
            zone_id=zone_id,
            zone_name=zone_meta.get("zone_name"),
            floor=zone_meta.get("floor"),
            camera_id=camera_id,
            current_count=0,
            max_capacity=zone_meta.get("max_capacity") or settings.DEFAULT_ZONE_CAPACITY,
            last_updated=facility_now_naive(),
        )
        db.add(zone)
        db.flush()
    else:
        if zone_meta.get("zone_name") and not zone.zone_name:
            zone.zone_name = zone_meta["zone_name"]
        if zone_meta.get("floor") and not zone.floor:
            zone.floor = zone_meta["floor"]
        if zone_meta.get("max_capacity") and (
            zone.max_capacity is None or zone.max_capacity == settings.DEFAULT_ZONE_CAPACITY
        ):
            zone.max_capacity = zone_meta["max_capacity"]
    return zone


def reconcile_zone_counts_from_open_sessions(
    db: Session,
    *,
    camera_id: str,
) -> dict[str, int]:
    """Set every aggregate from the existing open-session source of truth.

    Line events are wake-up signals only. They never add or subtract counts, so
    duplicate/missed crossings cannot accumulate drift. The same helper is
    called by entry and exit transactions before commit.
    """
    counts: dict[str, int] = {}
    for zone_id in (
        settings.GARAGE_TOTAL_ZONE,
        settings.B1_PARKING_ZONE,
        settings.B2_PARKING_ZONE,
    ):
        zone = _ensure_zone(db, zone_id, camera_id)
        query = db.query(func.count(ParkingSession.id)).filter(
            ParkingSession.status == "open"
        )
        floor = zone.floor or settings.get_zone_metadata(zone_id).get("floor")
        if zone_id != settings.GARAGE_TOTAL_ZONE and floor:
            query = query.filter(ParkingSession.floor == floor)
        count = int(query.scalar() or 0)
        zone.current_count = count
        zone.last_updated = facility_now_naive()
        counts[zone_id] = count
        logger.info(
            "[UC3] %s reconciled from open sessions: %s/%s",
            zone_id,
            count,
            zone.max_capacity,
        )
    db.flush()
    return counts


async def _check_capacity_alert(
    db: Session,
    zone_id: str,
    camera_id: str,
    snapshot_path: str = None,
) -> None:
    zone = _ensure_zone(db, zone_id, camera_id)

    real_max, real_occupancy = await _real_capacity_state(db, zone_id, zone.floor)
    if real_max > 0 and real_occupancy > real_max:
        real_pct = int((real_occupancy / real_max) * 100)
        now_ts = facility_now_naive().timestamp()
        cooldown_key = (camera_id, zone_id)
        last_alert_ts = _capacity_alert_cache.get(cooldown_key, 0)
        if now_ts - last_alert_ts >= CAPACITY_ALERT_COOLDOWN:
            _capacity_alert_cache[cooldown_key] = now_ts
            scope = "Garage" if zone_id == settings.GARAGE_TOTAL_ZONE else f"Floor {zone.floor or zone_id}"
            await create_alert(
                db,
                alert_type="capacity_exceeded",
                camera_id=camera_id,
                zone_id=zone_id,
                zone_name=zone.zone_name,
                event_type="occupancy_update",
                description=(
                    f"{scope} capacity exceeded: {real_pct}% "
                    f"({real_occupancy} parked / {real_max} slots)"
                ),
                snapshot_path=snapshot_path,
            )
        else:
            remaining = int(CAPACITY_ALERT_COOLDOWN - (now_ts - last_alert_ts))
            logger.debug(f"[UC3] {camera_id}/{zone_id}: capacity_exceeded alert suppressed (cooldown {remaining}s remaining)")


def _cache_cleanup():
    """
    FIX #4 (cache memory leak): evict entries older than CACHE_TTL_SECONDS.
    Called on every new cache insert to bound memory growth over long uptimes.
    """
    now = facility_now_naive().timestamp()
    expired = [k for k, v in _processed_events_cache.items() if now - v >= CACHE_TTL_SECONDS]
    for k in expired:
        del _processed_events_cache[k]


def record_event_in_cache(cache_key: tuple):
    """
    FIX #2 (cache-vs-rollback): public helper called by the router AFTER db.commit()
    succeeds. This ensures the cache only records events whose DB changes actually persisted.
    If the transaction rolls back, the cache stays clean and camera retries are accepted.
    """
    _cache_cleanup()  # FIX #4: prune stale entries on every insert
    _processed_events_cache[cache_key] = facility_now_naive().timestamp()


async def handle_occupancy_event(event: ParsedCameraEvent, db: Session):
    """
    UC3: Updates multi-level occupancy counts.
    Handles GARAGE-TOTAL, B1-PARKING, and B2-PARKING.

    Returns the cache_key if the event was processed (caller must call
    record_event_in_cache after commit), or None if the event was filtered/deduped.
    """
    # ── STRICT FILTERING ────────────────────────────────────────────────
    # 1. Target must be 'vehicle'
    if event.detection_target != "vehicle":
        logger.debug(f"[UC3] Ignoring {event.event_type} event from {event.camera_id}: target={event.detection_target} (expected 'vehicle')")
        return None

    # 2. Event type must be an occupancy trigger
    allowed_types = ("linedetection",)
    if event.event_type not in allowed_types:
        logger.debug(f"[UC3] Ignoring {event.event_type} event from {event.camera_id}: type not in {allowed_types}")
        return None

    # 3. Deduplication: Ignore identical events within CACHE_TTL_SECONDS
    # FIX #5: uses EVENT_STREAM_SUPPRESS_SECONDS (30s) instead of hardcoded 1s
    cache_key = (event.camera_id, event.event_type, event.region_id or event.crossing_direction)
    now = facility_now_naive().timestamp()
    if cache_key in _processed_events_cache:
        last_time = _processed_events_cache[cache_key]
        if now - last_time < CACHE_TTL_SECONDS:
            logger.debug(f"[UC3] {event.camera_id}: DROPPED duplicate {event.event_type} event (within {CACHE_TTL_SECONDS}s window)")
            return None
    # FIX #2: Do NOT write to cache here. The caller (router or test) must call
    # record_event_in_cache(cache_key) AFTER db.commit() succeeds.
    # ───────────────────────────────────────────────────────────────────

    cam_id = event.camera_id

    # ── DETERMINE DIRECTION MULTIPLIER ────────────────────────────────────
    # +1 = primary/forward direction for this camera
    # -1 = reverse direction for this camera
    #
    # Source priority:
    #   1. region_id: "1" = forward (+1), "2" = reverse (−1)
    #   2. crossing_direction: "B-to-A" = forward (+1), "A-to-B" = reverse (−1)
    #   3. No signal at all → REJECT the event (better to skip than count wrong)
    # ──────────────────────────────────────────────────────────────────────

    multiplier = None

    if event.region_id == settings.OCCUPANCY_ENTRANCE_ZONES:       # "1"
        multiplier = 1
        logger.info(f"[UC3] {cam_id}: Line {event.region_id} (forward) → mult: +1")
    elif event.region_id == settings.OCCUPANCY_EXIT_ZONES:          # "2"
        multiplier = -1
        logger.info(f"[UC3] {cam_id}: Line {event.region_id} (reverse) → mult: -1")
    elif event.crossing_direction == "B-to-A":
        multiplier = 1
        logger.info(f"[UC3] {cam_id}: dir=B-to-A (forward) → mult: +1")
    elif event.crossing_direction == "A-to-B":
        multiplier = -1
        logger.info(f"[UC3] {cam_id}: dir=A-to-B (reverse) → mult: -1")
    else:
        logger.warning(
            f"[UC3] {cam_id}: SKIPPED — no direction signal "
            f"(region_id={event.region_id!r}, crossing_direction={event.crossing_direction!r}). "
            f"Cannot determine entry/exit; ignoring event to avoid wrong count."
        )
        return None
    # ──────────────────────────────────────────────────────────────────────

    # A line crossing triggers a full reconciliation; it never pushes a delta.
    # All rows update in one savepoint so readers cannot observe mixed floors.
    savepoint = db.begin_nested()
    try:
        reconcile_zone_counts_from_open_sessions(db, camera_id=cam_id)
        for zone_id in (
            settings.GARAGE_TOTAL_ZONE,
            settings.B1_PARKING_ZONE,
            settings.B2_PARKING_ZONE,
        ):
            await _check_capacity_alert(
                db,
                zone_id,
                cam_id,
                event.snapshot_path,
            )
        if cam_id == "CAM-03":
            if (
                multiplier == 1
                and settings.ENTRY_V2_MODE != "authoritative"
            ):  # legacy entry direction only — confirm pending ANPR
                from app.services.entry_exit_service import confirm_pending_entry
                # Pass CAM-03's own snapshot so it can be forwarded to the PMS
                # API (under the B-entry marker) once the plate is known.
                await confirm_pending_entry(
                    db,
                    cam03_snapshot=event.local_snapshot_path or event.snapshot_path,
                )
        savepoint.commit()
    except Exception as e:
        savepoint.rollback()
        logger.error(f"[UC3] {cam_id}: Zone update failed, savepoint rolled back — no drift: {e}")
        return None

    # Final Summary Log: Always show all zone statuses for full context
    all_zones = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id.in_([
        settings.GARAGE_TOTAL_ZONE,
        settings.B1_PARKING_ZONE,
        settings.B2_PARKING_ZONE
    ])).all()

    zone_order = {
        settings.GARAGE_TOTAL_ZONE: 0,
        settings.B1_PARKING_ZONE: 1,
        settings.B2_PARKING_ZONE: 2
    }
    all_zones.sort(key=lambda z: zone_order.get(z.zone_id, 99))

    summary_parts = []
    for z in all_zones:
        pct = int((z.current_count / z.max_capacity * 100) if z.max_capacity > 0 else 0)
        summary_parts.append(f"{z.zone_id}: {z.current_count}/{z.max_capacity} ({pct}%)")

    if summary_parts:
        logger.info(f"[UC3] OVERALL STATUS | {' | '.join(summary_parts)}")

    return cache_key
