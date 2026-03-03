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

# Maps the camera's numeric regionID (1, 2, 3, 4) → logical zone name.
# These numbers come from the camera's Field Detection / Region Entrance rules
# and cannot be renamed on the camera UI — so we translate them here.
REGION_ID_TO_ZONE = {
    "1": "emergency-exit",
    "2": "staff-only-area",
    "3": "after-hours-zone",
}

MONITORED_INTRUSION_ZONES = set(REGION_ID_TO_ZONE.values())


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    # Translate numeric regionID → zone name; fall back to camera-field default
    raw_region = event.region_id
    zone_id = REGION_ID_TO_ZONE.get(raw_region) if raw_region else None
    if zone_id is None:
        if raw_region is not None:
            # Received a region we don't monitor — ignore silently
            return
        zone_id = f"{event.camera_id}-field"  # VMD / fielddetection with no region

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
