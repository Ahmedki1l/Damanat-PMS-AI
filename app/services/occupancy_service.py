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
_pending_exits = {} # Format: {event_key: (zone_id, delta, timestamp)}
CACHE_TTL_SECONDS = 5

async def _update_zone_count(zone_id: str, camera_id: str, delta: int, db: Session):
    """
    Update a zone's occupancy count.
    If NODEBACK_URL is configured: POST to Node.js and check isFull for capacity alert.
    Fallback: write directly to local zone_occupancy table.
    """
    from app.utils import core_backend_client

    zone_uuid = settings.ZONE_NAME_TO_UUID.get(zone_id)

    if settings.NODEBACK_URL and zone_uuid:
        # ── HTTP push to Node.js ──────────────────────────────────────────
        if delta > 0:
            data = await core_backend_client.notify_occupancy_entry(zone_uuid, camera_id)
        else:
            data = await core_backend_client.notify_occupancy_exit(zone_uuid, camera_id)

        if data:
            current = data.get("currentCount", 0)
            max_cap = data.get("maxCapacity", 0)
            pct = data.get("percentage", 0)
            logger.debug(f"[UC3] {zone_id}: {current}/{max_cap} ({pct}%)")

            if data.get("isFull"):
                await create_alert(
                    db,
                    alert_type="capacity_exceeded",
                    camera_id=camera_id,
                    zone_id=zone_uuid,
                    event_type="occupancy_update",
                    description=f"Zone {zone_id} is full: {current}/{max_cap} (100%)",
                )
        return

    # ── Fallback: write to local zone_occupancy table ─────────────────────
    from app.models.zone_occupancy import ZoneOccupancy

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

    zone.current_count = max(0, zone.current_count + delta)
    occupancy_ratio = (zone.current_count / zone.max_capacity) if zone.max_capacity > 0 else 0
    pct = int(occupancy_ratio * 100)
    logger.debug(f"[UC3] {zone_id}: {zone.current_count}/{zone.max_capacity} ({pct}%)")

    if occupancy_ratio >= settings.OCCUPANCY_ALERT_THRESHOLD:
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
    allowed_types = ("linedetection", "ANPR", "AccessControllerEvent", "vehicleMatchResult")
    if event.event_type not in allowed_types:
        logger.debug(f"[UC3] Ignoring {event.event_type} event from {event.camera_id}: type not in {allowed_types}")
        return
    # ───────────────────────────────────────────────────────────────────

    id_ref = event.plate_number or event.region_id or "default"
    cam_id = event.camera_id
    
    # We define the event_key to keep track of zones for the entry-exit confirm window
    event_key = (cam_id, event.event_type, id_ref, event.crossing_direction)

    multiplier = 1
    if event.crossing_direction and event.crossing_direction != settings.FORWARD_DIRECTION_FIELD:
        multiplier = -1
        logger.debug(f"[UC3] Reverse crossing detected on {cam_id} (mult: -1)")
    # ───────────────────────────────────────────────────────────────────

    cam_config = settings.CAMERAS.get(cam_id, {})
    
    # (Option A check is now implicit via multiplier + cancellation logic below)

    # ── CONFIRMATION WINDOW (Option B - Setup helper) ────────────────
    async def _queue_or_apply(zone_id: str, delta: int, skip_window: bool = False):
        # We use a combined key to allow multiple zones from the same event to be pending
        full_key = (event_key, zone_id)
        
        if delta < 0 and settings.USE_EXIT_CONFIRM_WINDOW and not skip_window:
            logger.info(f"[UC3] {zone_id}: Exit pending {settings.EXIT_CONFIRM_SECONDS}s confirmation (vehicle reversing?)")
            _pending_exits[full_key] = (zone_id, cam_id, delta, datetime.utcnow())
        else:
            if delta > 0:
                cancelled_zones = []
                # logger.debug(f"[UC3] Entry detected on {cam_id}, checking {len(_pending_exits)} pending exits...")
                for k in list(_pending_exits.keys()):
                    # Match by camera_id. k is ((cam_id, type, id, dir), zone_id)
                    pending_cam = k[0][0]
                    pending_key = k[0]
                    
                    if pending_cam == cam_id:
                        # Avoid self-cancellation: don't cancel a pending exit from the SAME trigger
                        if pending_key == event_key:
                            continue
                        
                        del _pending_exits[k]
                        cancelled_zones.append(k[1])
                
                if cancelled_zones:
                    unique_zones = list(set(cancelled_zones))
                    logger.info(f"[UC3] Reversed exit cancelled for {cam_id} on: {', '.join(unique_zones)}")
                    return 

            await _update_zone_count(zone_id, cam_id, delta, db)
    # ───────────────────────────────────────────────────────────────────

    # ── MULTI-ZONE ROUTING LOGIC ──────────────────────────────────────
    
    # Case 0: Main ANPR Entrance Gate (Phase 2 Entry)
    if cam_id == "CAM-ENTRY":
        await _queue_or_apply(settings.GARAGE_TOTAL_ZONE, 1 * multiplier)
        await _queue_or_apply(settings.B1_PARKING_ZONE, 1 * multiplier)

    # Case 0.1: Main ANPR Exit Gate (Phase 2 Exit)
    elif cam_id == "CAM-EXIT":
        await _queue_or_apply(settings.GARAGE_TOTAL_ZONE, -1 * multiplier)
        await _queue_or_apply(settings.B1_PARKING_ZONE, -1 * multiplier)

    # Case 1: Main Entrance Gate (CAM-03 Internal)
    elif cam_id == "CAM-03":
        await _queue_or_apply(settings.GARAGE_TOTAL_ZONE, 1 * multiplier)
        await _queue_or_apply(settings.B1_PARKING_ZONE, 1 * multiplier)

    # Case 2: Main Exit Gate (CAM-08 Internal)
    elif cam_id == "CAM-08":
        await _queue_or_apply(settings.GARAGE_TOTAL_ZONE, -1 * multiplier)
        await _queue_or_apply(settings.B1_PARKING_ZONE, -1 * multiplier)

    # Case 3: Transition B1 -> B2 (CAM-09)
    # Transitions are internal - skip confirmation window for immediate feedback
    elif cam_id == "CAM-09":
        await _queue_or_apply(settings.B1_PARKING_ZONE, -1 * multiplier, skip_window=True)
        await _queue_or_apply(settings.B2_PARKING_ZONE, 1 * multiplier)

    # Case 4: Transition B2 -> B1 (CAM-10)
    elif cam_id == "CAM-10":
        await _queue_or_apply(settings.B2_PARKING_ZONE, -1 * multiplier, skip_window=True)
        await _queue_or_apply(settings.B1_PARKING_ZONE, 1 * multiplier)


    # Case 5: Legacy/Phase 1 Fallback (e.g. CAM-04 or others during debug)
    # Only processes if specially triggered by dispatcher
    elif event.event_type in ("ANPR", "vehicleMatchResult", "AccessControllerEvent", "linedetection"):
        cam_config = settings.CAMERAS.get(cam_id, {})
        gate = cam_config.get("gate")
        
        # If no specific multi-zone rule, update the camera's primary zone
        delta = 1 if gate == "entry" else (-1 if gate == "exit" else 0)
        if delta != 0:
            zone_id = cam_config.get("name") or settings.B1_PARKING_ZONE
            await _update_zone_count(zone_id, cam_id, delta, db)

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


async def process_pending_exits(db: Session):
    """
    Background worker to confirm exits that passed the confirmation window.
    Should be called periodically or after each event.
    """
    now = datetime.utcnow()
    to_confirm = []
    
    for key, (zone_id, cam_id, delta, ts) in list(_pending_exits.items()):
        if (now - ts).total_seconds() > settings.EXIT_CONFIRM_SECONDS:
            to_confirm.append((key, zone_id, cam_id, delta))
            
    for key, zone_id, cam_id, delta in to_confirm:
        del _pending_exits[key]
        await _update_zone_count(zone_id, cam_id, delta, db)

    if to_confirm:
        # Show overall status after confirmation for context
        all_zones = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id.in_([
            settings.GARAGE_TOTAL_ZONE, 
            settings.B1_PARKING_ZONE, 
            settings.B2_PARKING_ZONE
        ])).all()
        zone_order = {settings.GARAGE_TOTAL_ZONE: 0, settings.B1_PARKING_ZONE: 1, settings.B2_PARKING_ZONE: 2}
        all_zones.sort(key=lambda z: zone_order.get(z.zone_id, 99))
        
        summary_parts = []
        for z in all_zones:
            pct = int((z.current_count / z.max_capacity * 100) if z.max_capacity > 0 else 0)
            summary_parts.append(f"{z.zone_id}: {z.current_count}/{z.max_capacity} ({pct}%)")
        
        if summary_parts:
            logger.info(f"[UC3] OVERALL STATUS AFTER CONFIRMATION | {' | '.join(summary_parts)}")