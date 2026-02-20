# app/services/violation_service.py
"""
UC5: Proactive Violation Alerts
Events: fielddetection (restricted zone), linedetection (forbidden line), regionEntrance
Config: Add zone IDs matching camera-configured zone names to RESTRICTED_ZONES
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

RESTRICTED_ZONES = {"restricted-vip", "no-parking-zone", "emergency-exit", "loading-bay"}
ALWAYS_VIOLATION_EVENTS = {"linedetection"}


async def handle_violation_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or "unknown-zone"
    if zone_id not in RESTRICTED_ZONES and event.event_type not in ALWAYS_VIOLATION_EVENTS:
        return

    cooldown = timedelta(seconds=settings.INTRUSION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id, Alert.alert_type == "violation",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    if recent:
        return

    desc = (f"Line crossing in zone {zone_id}" if event.event_type == "linedetection"
            else f"Vehicle in restricted zone: {zone_id}")
    logger.warning(f"[UC5] VIOLATION: {desc}")
    await create_alert(db, "violation", event.camera_id, zone_id, event.event_type, desc)
