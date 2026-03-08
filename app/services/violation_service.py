# app/services/violation_service.py
"""
UC5: Proactive Violation Alerts
Events: fielddetection (restricted zone), linedetection (forbidden line), regionEntrance
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

RESTRICTED_ZONES = {
    ZoneNames.Violation.RESTRICTED_VIP,
    ZoneNames.Violation.NO_PARKING_ZONE,
    ZoneNames.Violation.EMERGENCY_EXIT,
    ZoneNames.Violation.LOADING_BAY,
}

ALWAYS_VIOLATION_EVENTS = {"linedetection"}


async def handle_violation_event(event: ParsedCameraEvent, db: Session):
    # Extracts detectionTarget = vehicle (must be vehicle to proceed)
    if event.detection_target and event.detection_target.lower() != "vehicle":
        return

    zone_id = resolve_zone(event.camera_id, event.region_id) or event.region_id or "unknown-zone"

    # Zone check: Is regionID in RESTRICTED_ZONES? Or is the event type linedetection (always a violation)?
    if zone_id not in RESTRICTED_ZONES and event.event_type != "linedetection":
        return

    # Cooldown check: Query alerts table — is there already a violation alert for this zone within the last N seconds?
    cooldown = timedelta(seconds=settings.VIOLATION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id,
        Alert.alert_type == "violation",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    
    if recent:
        # suppress duplicate, return
        return

    # Create alert
    desc = (f"Line crossing in zone {zone_id}" if event.event_type == "linedetection"
            else f"Vehicle in restricted zone: {zone_id}")
    logger.warning(f"[UC5] VIOLATION: {desc}")
    await create_alert(db, "violation", event.camera_id, zone_id, event.event_type, desc)
