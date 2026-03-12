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

async def _update_zone_count(zone_id: str, camera_id: str, delta: int, db: Session):
    """
    Update a zone's occupancy count in the local database first, then try to
    sync the same increment/decrement to the Node.js backend.
    """
    from app.utils import core_backend_client

    zone_uuid = settings.ZONE_NAME_TO_UUID.get(zone_id)

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
    logger.debug(f"[UC3] {zone_id}: {zone.current_count} {delta} ({zone.current_count + delta}) 1")

    zone.camera_id = camera_id
    zone.current_count = max(0, zone.current_count + delta)
    logger.debug(f"[UC3] {zone_id}: {zone.current_count} {delta} 2")
    zone.last_updated = datetime.utcnow()
    occupancy_ratio = (zone.current_count / zone.max_capacity) if zone.max_capacity > 0 else 0
    pct = int(occupancy_ratio * 100)
    logger.debug(f"[UC3] {zone_id}: {zone.current_count}/{zone.max_capacity} ({pct}%)")
    logger.debug(f"[UC3] {zone_id}: {zone.current_count} {delta} 3")

    if occupancy_ratio >= 1:
        await create_alert(
            db,
            alert_type="capacity_exceeded",
            camera_id=camera_id,
            zone_id=zone_id,
            event_type="occupancy_update",
            description=f"Zone {zone_id} is nearly full: {pct}% ({zone.current_count}/{zone.max_capacity})",
        )

    logger.debug(f"[UC3] {zone_id}: {zone.current_count} {delta} 4")

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
    # ───────────────────────────────────────────────────────────────────

    cam_id = event.camera_id
    
    multiplier = 1
    # Priority 1: Use Line IDs (Region 1 = Entry/Forward, Region 2 = Exit/Reverse)
    if event.event_type == "linedetection" and event.region_id:
        if event.region_id == settings.OCCUPANCY_ENTRANCE_ZONES:
            multiplier = 1
            logger.info(f"[UC3] {cam_id}: Line {event.region_id} (Entry) -> mult: +1")
        elif event.region_id == settings.OCCUPANCY_EXIT_ZONES:
            multiplier = -1
            logger.info(f"[UC3] {cam_id}: Line {event.region_id} (Exit) -> mult: -1")
        elif event.crossing_direction and event.crossing_direction != "B-to-A":
            multiplier = -1
            logger.info(f"[UC3] {cam_id}: Reverse direct ({event.crossing_direction}) -> mult: -1")
        else:
            logger.info(f"[UC3] {cam_id}: Forward direct ({event.crossing_direction}) via Line {event.region_id} -> mult: +1")
    # Priority 2: Fallback to simplified direction logic
    elif event.crossing_direction and event.crossing_direction != "B-to-A":
        multiplier = -1
        logger.info(f"[UC3] {cam_id}: Fallback reverse ({event.crossing_direction}) -> mult: -1")
    else:
        logger.info(f"[UC3] {cam_id}: Fallback forward ({event.crossing_direction}) -> mult: +1")
    # ───────────────────────────────────────────────────────────────────

    cam_config = settings.CAMERAS.get(cam_id, {})
    
    # (Option A check is now implicit via multiplier + cancellation logic below)

    # Confirmation window removed per user request for immediate feedback
    # ───────────────────────────────────────────────────────────────────

    # ── MULTI-ZONE ROUTING LOGIC ──────────────────────────────────────
    
    # Case 1: Main Entrance Gate (CAM-03 Internal)
    if cam_id == "CAM-03":
        await _update_zone_count(settings.GARAGE_TOTAL_ZONE, cam_id, 1 * multiplier, db)
        await _update_zone_count(settings.B1_PARKING_ZONE, cam_id, 1 * multiplier, db)
    # Case 2: Main Exit Gate (CAM-08 Internal)
    elif cam_id == "CAM-08":
        await _update_zone_count(settings.GARAGE_TOTAL_ZONE, cam_id, -1 * multiplier, db)
        await _update_zone_count(settings.B1_PARKING_ZONE, cam_id, -1 * multiplier, db)
 
    # Case 3: Transition B1 -> B2 (CAM-09)
    # Transitions are internal
    elif cam_id == "CAM-09":
        await _update_zone_count(settings.B1_PARKING_ZONE, cam_id, -1 * multiplier, db)
        await _update_zone_count(settings.B2_PARKING_ZONE, cam_id, 1 * multiplier, db)
 
    # Case 4: Transition B2 -> B1 (CAM-10)
    elif cam_id == "CAM-10":
        await _update_zone_count(settings.B2_PARKING_ZONE, cam_id, -1 * multiplier, db)
        await _update_zone_count(settings.B1_PARKING_ZONE, cam_id, 1 * multiplier, db)


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


