# app/services/violation_service.py
"""
UC5: Proactive Violation Alerts
Events: fielddetection (restricted zone), linedetection (forbidden line), regionEntrance

Zone resolution priority:
  1. zone_config.resolve_zone() — translates camera slot IDs → canonical names
  2. settings.RESTRICTED_ZONES — defines which canonical names trigger alerts
     (overridable via .env without code changes; defaults mirror ZoneNames.Violation constants)
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.zone_config import resolve_zone
from app.utils.logger import get_logger

logger = get_logger(__name__)


def _get_restricted_zones() -> set:
    """Load restricted zone names from settings (env-overridable)."""
    return set(z.strip() for z in settings.RESTRICTED_ZONES.split(",") if z.strip())


def _get_always_violation_events() -> set:
    """Load event types that are always violations (env-overridable)."""
    return set(e.strip() for e in settings.ALWAYS_VIOLATION_EVENTS.split(",") if e.strip())


# Module-level aliases for dispatcher import — reflect current settings values.
RESTRICTED_ZONES = _get_restricted_zones()
ALWAYS_VIOLATION_EVENTS = _get_always_violation_events()


async def handle_violation_event(event: ParsedCameraEvent, db: Session):
    if event.detection_target and event.detection_target.lower() != "vehicle":
        return

    # Resolve slot ID → canonical zone name via zone_config, fall back to raw region_id
    canonical_zone = resolve_zone(event.camera_id, event.region_id)
    zone_id = canonical_zone or event.region_id or "unknown-zone"

    restricted = _get_restricted_zones()
    always_events = _get_always_violation_events()

    if zone_id not in restricted and event.event_type not in always_events:
        return

    cooldown = timedelta(seconds=settings.VIOLATION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id,
        Alert.alert_type == "violation",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    if recent:
        return

    desc = (f"Line crossing in zone {zone_id}" if event.event_type == "linedetection"
            else f"Vehicle in restricted zone: {zone_id}")
    logger.warning(f"[UC5] VIOLATION: {desc}")
    alert_zone_id = settings.CAMERA_ZONE_MAP.get(event.camera_id, zone_id)
    await create_alert(db, "violation", event.camera_id, alert_zone_id, event.event_type, desc)


async def resolve_violation_on_exit(camera_id: str, zone_id: str, db: Session):
    """Auto-resolve the latest open violation when vehicle exits the restricted zone."""
    alert = (
        db.query(Alert)
        .filter(
            Alert.camera_id == camera_id,
            Alert.zone_id == zone_id,
            Alert.is_resolved == False,
        )
        .order_by(Alert.triggered_at.desc())
        .first()
    )
    if alert:
        alert.is_resolved = True
        alert.resolved_at = datetime.utcnow()
        db.commit()
        logger.info(f"[ViolationService] Auto-resolved violation {alert.id} — vehicle exited {zone_id}")
    
