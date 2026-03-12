# app/services/intrusion_service.py
"""
UC6: Intrusion Detection
Events: fielddetection, regionEntrance - vehicle only.
Handles zone resolution and alert cooldowns.
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.zone_config import ZoneNames, resolve_zone
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Structured list of zones that trigger intrusion alerts
MONITORED_INTRUSION_ZONES = {
    ZoneNames.Intrusion.EMERGENCY_EXIT,
    ZoneNames.Intrusion.STAFF_ONLY_AREA,
    ZoneNames.Intrusion.AFTER_HOURS_ZONE,
}

# In-memory session tracker: (camera_id, zone_id) → datetime of first event (UTC naive)
_active_sessions: dict[tuple[str, str], datetime] = {}


def clear_session(camera_id: str, zone_id: str):
    """Clear session on zone exit."""
    _active_sessions.pop((camera_id, zone_id), None)


def _parse_region_id(raw_region_id: str | None) -> int | None:
    if raw_region_id is None:
        return None
    if isinstance(raw_region_id, int):
        return raw_region_id
    if isinstance(raw_region_id, str) and raw_region_id.strip().isdigit():
        return int(raw_region_id.strip())
    return None


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    """
    Processes smart events to detect unauthorized vehicle presence.
    Uses resolve_zone to map camera IDs to specific logical areas.
    """
    # 1. Resolve logical zone from camera config
    zone_id = resolve_zone(event.camera_id, event.region_id)
    region_id = _parse_region_id(event.region_id)

    # Fallback: if no specific region is mapped, use a generic field-of-view ID
    if zone_id is None:
        if event.region_id is not None:
            # If it's a specific region but not in our mapping, ignore it
            return
        zone_id = f"{event.camera_id}-field"

    # 2. Filter: Only process zones explicitly listed as monitored
    if zone_id not in MONITORED_INTRUSION_ZONES and not zone_id.endswith("-field"):
        return

    # Session-based deduplication
    key = (event.camera_id, zone_id)
    now = datetime.utcnow()
    session_start = _active_sessions.get(key)

    if session_start is not None:
        elapsed = (now - session_start).total_seconds()
        if elapsed < settings.EVENT_STREAM_SUPPRESS_SECONDS:
            logger.debug(f"[UC6] Suppressed duplicate: {key} ({elapsed:.0f}s into session)")
            return
        if elapsed < settings.EVENT_STREAM_MAX_DURATION_SECONDS:
            logger.debug(f"[UC6] Suppressed duplicate: {key} ({elapsed:.0f}s into session)")
            return
        # Session expired — reset and treat as new event
        logger.info(f"[UC6] Session reset for {key} after {elapsed:.0f}s")
        del _active_sessions[key]

    # DB cooldown check (protects against restarts losing in-memory state)
    cooldown = timedelta(seconds=settings.INTRUSION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id, Alert.alert_type == "intrusion",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()

    if recent:
        logger.debug(f"Intrusion in {zone_id} skipped due to cooldown.")
        return

    # First event in session — process and start tracking
    _active_sessions[key] = now
    desc = f"Vehicle intrusion in {zone_id} — {event.camera_id}"
    logger.warning(f"[UC6] INTRUSION: {desc}")
    await create_alert(db, "intrusion", event.camera_id, zone_id, event.event_type, desc)
    # 4. Create Alert
    description = f"Vehicle intrusion detected in {zone_id} via {event.camera_id}"
    logger.warning(f"[UC6] INTRUSION DETECTED: {description}")
    alert_zone_id = settings.CAMERA_ZONE_MAP.get(event.camera_id, zone_id)
    await create_alert(
        db,
        alert_type="intrusion",
        camera_id=event.camera_id,
        zone_id=zone_id,
        event_type=event.event_type,
        description=description,
        snapshot_path=event.snapshot_path,
        region_id=region_id,
    )
