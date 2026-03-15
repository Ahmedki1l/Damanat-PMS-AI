# app/services/occupancy_service.py
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.zone_occupancy import ZoneOccupancy
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Simple in-memory cache to prevent double-counting from identical events fired within seconds
# Format: {(camera_id, event_type, plate_or_region): timestamp}
_processed_events_cache = {}
CACHE_TTL_SECONDS = 1

async def push_db_update(db: Session, zone_id: str, delta: int):
    """
    Atomic 'Push' function to update the database directly.
    This replaces the old ORM update to prevent miscounts.
    """
    from sqlalchemy import update
    
    stmt = (
        update(ZoneOccupancy)
        .where(ZoneOccupancy.zone_id == zone_id)
        .values(current_count=ZoneOccupancy.current_count + delta)
    )
    db.execute(stmt)
    db.flush() # Explicitly push the change to the database session

async def _update_zone_count(zone_id: str, camera_id: str, delta: int, db: Session):
    """
    Update a zone's occupancy count using the manual push function.
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

    # 1. Use the 'push' function to update the count atomically
    await push_db_update(db, zone_id, delta)
    
    # 2. Refresh to get the final count for logging/alerts
    db.refresh(zone)
    
    # Ensure count doesn't drop below zero
    if zone.current_count < 0:
        zone.current_count = 0
        db.flush()

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

async def handle_occupancy_event(event: ParsedCameraEvent, db: Session):
    """
    UC3: Updates multi-level occupancy counts.
    Handles GARAGE-TOTAL, B1-PARKING, and B2-PARKING.
    """
    # ── STRICT FILTERING ────────────────────────────────────────────────
    # 1. Target must be 'vehicle'
    if event.detection_target != "vehicle":
        logger.debug(f"[UC3] Ignoring {event.event_type} event from {event.camera_id}: target={event.detection_target} (expected 'vehicle')")
        return
    
    # 2. Event type must be an occupancy trigger
    allowed_types = ("linedetection",)
    if event.event_type not in allowed_types:
        logger.debug(f"[UC3] Ignoring {event.event_type} event from {event.camera_id}: type not in {allowed_types}")
        return

    # 3. Deduplication: Ignore identical events within 1 second
    # Use (cam, type, region) or (cam, type, direction) as the key
    cache_key = (event.camera_id, event.event_type, event.region_id or event.crossing_direction)
    now = datetime.utcnow().timestamp()
    if cache_key in _processed_events_cache:
        last_time = _processed_events_cache[cache_key]
        if now - last_time < CACHE_TTL_SECONDS:
            logger.debug(f"[UC3] {event.camera_id}: DROPPED duplicate {event.event_type} event (too fast)")
            return
    _processed_events_cache[cache_key] = now
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
        return
    # ──────────────────────────────────────────────────────────────────────


    cam_config = settings.CAMERAS.get(cam_id, {})

    # Confirmation window removed per user request for immediate feedback
    # ───────────────────────────────────────────────────────────────────

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

    # Case 1: Main Entrance Gate (CAM-03)
    if cam_id == "CAM-03":
        await _update_zone_count(settings.GARAGE_TOTAL_ZONE, cam_id,  1 * multiplier, db)
        await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id,  1 * multiplier, db)
    # Case 2: Main Exit Gate (CAM-08)
    elif cam_id == "CAM-08":
        await _update_zone_count(settings.GARAGE_TOTAL_ZONE, cam_id, -1 * multiplier, db)
        await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id, -1 * multiplier, db)
    # Case 3: Transition B1 → B2 (CAM-09)
    elif cam_id == "CAM-09":
        await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id, -1 * multiplier, db)
        await _update_zone_count(settings.B2_PARKING_ZONE,   cam_id,  1 * multiplier, db)
    # Case 4: Transition B2 → B1 (CAM-10)
    elif cam_id == "CAM-10":
        await _update_zone_count(settings.B2_PARKING_ZONE,   cam_id, -1 * multiplier, db)
        await _update_zone_count(settings.B1_PARKING_ZONE,   cam_id,  1 * multiplier, db)


    # Final Summary Log: Always show all zone statuses for full context
    all_zones = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id.in_([
        settings.GARAGE_TOTAL_ZONE, 
        settings.B1_PARKING_ZONE, 
        settings.B2_PARKING_ZONE
    ])).all()
    
    # Sort for consistent display: Total, then GF, then B1, then B2
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


