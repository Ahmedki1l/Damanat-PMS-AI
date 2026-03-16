# app/services/occupancy_service.py
from datetime import datetime
from sqlalchemy import update, func
from sqlalchemy.orm import Session
from app.models.zone_occupancy import ZoneOccupancy
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
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


async def push_db_update(db: Session, zone_id: str, delta: int):
    """
    Atomic 'Push' function to update the database directly.
    FIX #3 (clamp-to-zero race): uses func.max(..., 0) so the count can never
    go negative in a single atomic SQL statement. This eliminates the race window
    where a concurrent +1 between a non-atomic refresh and ORM clamp would be lost.
    """
    stmt = (
        update(ZoneOccupancy)
        .where(ZoneOccupancy.zone_id == zone_id)
        .values(current_count=func.max(ZoneOccupancy.current_count + delta, 0))
    )
    db.execute(stmt)
    db.flush()


async def _update_zone_count(zone_id: str, camera_id: str, delta: int, db: Session):
    """
    Update a zone's occupancy count using the atomic push function.
    """
    # ── Primary source of truth: local zone_occupancy table ───────────────
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        logger.info(f"[UC3] Auto-creating zone '{zone_id}'")
        zone = ZoneOccupancy(
            zone_id=zone_id,
            camera_id=camera_id,
            current_count=0,
            max_capacity=settings.DEFAULT_ZONE_CAPACITY,
            last_updated=datetime.utcnow(),
        )
        db.add(zone)
        db.flush()

    # 1. Atomic update: count = max(count + delta, 0) — never goes negative
    await push_db_update(db, zone_id, delta)

    # 2. Refresh to get the final count for logging/alerts
    db.refresh(zone)

    # FIX #3: removed non-atomic ORM clamp (if zone.current_count < 0: zone.current_count = 0)
    # The atomic func.max() in push_db_update now handles this.

    zone.last_updated = datetime.utcnow()
    occupancy_ratio = (zone.current_count / zone.max_capacity) if zone.max_capacity > 0 else 0
    pct = int(occupancy_ratio * 100)

    logger.info(f"[UC3] {zone_id} Updated via Push: {zone.current_count}/{zone.max_capacity} ({pct}%)")

    if occupancy_ratio >= 1:
        await create_alert(
            db,
            alert_type="capacity_exceeded",
            camera_id=camera_id,
            zone_id=zone_id,
            event_type="occupancy_update",
            description=f"Zone {zone_id} is nearly full: {pct}% ({zone.current_count}/{zone.max_capacity})",
        )


def _cache_cleanup():
    """
    FIX #4 (cache memory leak): evict entries older than CACHE_TTL_SECONDS.
    Called on every new cache insert to bound memory growth over long uptimes.
    """
    now = datetime.utcnow().timestamp()
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
    _processed_events_cache[cache_key] = datetime.utcnow().timestamp()


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
    now = datetime.utcnow().timestamp()
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

    # ── MULTI-ZONE ROUTING LOGIC ──────────────────────────────────────
    #
    # multiplier  = +1 →  primary / forward direction for this camera
    # multiplier  = -1 →  reverse  direction for this camera
    #
    # The delta signs below are set so that the "primary" direction
    # (mult = +1) produces the expected occupancy change per camera:
    #
    #   CAM-03: primary = enter garage  →  TOTAL+1, B1+1
    #             delta: (+1 * mult)
    #   CAM-08: primary = exit  garage  →  TOTAL-1, B1-1
    #             delta: (-1 * mult)  ← double-negation intentional
    #   CAM-09: primary = B1→B2        →  B1-1, B2+1
    #             delta: (-1 * mult), (+1 * mult)
    #   CAM-10: primary = B2→B1        →  B2-1, B1+1
    #             delta: (-1 * mult), (+1 * mult)
    # ───────────────────────────────────────────────────────────────────

    # FIX #1 (two-zone drift): all zone updates for a single event are wrapped
    # in a savepoint. If any update fails, the savepoint rolls back ALL zone
    # changes for this event — preventing one zone from drifting without the other.
    savepoint = db.begin_nested()
    try:
        if cam_id == "CAM-03":
            await _update_zone_count(settings.GARAGE_TOTAL_ZONE, cam_id,  1 * multiplier, db)
            await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id,  1 * multiplier, db)
        elif cam_id == "CAM-08":
            await _update_zone_count(settings.GARAGE_TOTAL_ZONE, cam_id, -1 * multiplier, db)
            await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id, -1 * multiplier, db)
        elif cam_id == "CAM-09":
            await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id, -1 * multiplier, db)
            await _update_zone_count(settings.B2_PARKING_ZONE,   cam_id,  1 * multiplier, db)
        elif cam_id == "CAM-10":
            await _update_zone_count(settings.B2_PARKING_ZONE,   cam_id, -1 * multiplier, db)
            await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id,  1 * multiplier, db)

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