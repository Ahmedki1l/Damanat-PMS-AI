# app/services/intrusion_service.py
"""
UC6: Intrusion Detection
Events: fielddetection, regionEntrance — vehicle only
Note: Cannot verify plate identity without ANPR (Phase 2).
      Authorization check by plate is added in Phase 2 via entry_exit_service.
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

MONITORED_INTRUSION_ZONES = {"emergency-exit", "staff-only-area", "after-hours-zone"}


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or f"{event.camera_id}-field"
    if zone_id not in MONITORED_INTRUSION_ZONES and event.region_id is not None:
        return

    cooldown = timedelta(seconds=settings.INTRUSION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id, Alert.alert_type == "intrusion",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    if recent:
        return

    desc = f"Vehicle intrusion in {zone_id} — {event.camera_id}"
    logger.warning(f"[UC6] INTRUSION: {desc}")
    await create_alert(db, "intrusion", event.camera_id, zone_id, event.event_type, desc)
