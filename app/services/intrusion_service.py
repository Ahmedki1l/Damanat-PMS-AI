<<<<<<< HEAD
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


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    zone_id = resolve_zone(event.camera_id, event.region_id)

    if zone_id is None:
        if event.region_id is not None:
            return
        zone_id = f"{event.camera_id}-field"

    if zone_id not in MONITORED_INTRUSION_ZONES and not zone_id.endswith("-field"):
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
=======
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


def _get_monitored_zones() -> set:
    return set(z.strip() for z in settings.MONITORED_INTRUSION_ZONES.split(",") if z.strip())


# Module-level alias for dispatcher import
MONITORED_INTRUSION_ZONES = _get_monitored_zones()


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or f"{event.camera_id}-field"
    monitored = _get_monitored_zones()
    if zone_id not in monitored and event.region_id is not None:
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
>>>>>>> origin/Amr
