# app/services/intrusion_service.py
"""
UC6: Intrusion Detection
Events: fielddetection, regionEntrance — vehicle only
Note: Cannot verify plate identity without ANPR (Phase 2).
      Authorization check by plate is added in Phase 2 via entry_exit_service.

Zone slots from cameras are resolved via resolve_zone() using ZONE_MAPPING in app/zone_config.py.
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


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    zone_id = resolve_zone(event.camera_id, event.region_id)

    if zone_id is None:
        if event.region_id is not None:
            return
        zone_id = f"{event.camera_id}-field"

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
        return

    # First event in session — process and start tracking
    _active_sessions[key] = now
    desc = f"Vehicle intrusion in {zone_id} — {event.camera_id}"
    logger.warning(f"[UC6] INTRUSION: {desc}")
    await create_alert(db, "intrusion", event.camera_id, zone_id, event.event_type, desc)
