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


def _parse_region_id(raw_region_id: str | int | None) -> int | None:
    if raw_region_id is None:
        return None
    if isinstance(raw_region_id, int):
        return raw_region_id
    if isinstance(raw_region_id, str) and raw_region_id.strip().isdigit():
        return int(raw_region_id.strip())
    return None


# Module-level aliases for dispatcher import — reflect current settings values.
RESTRICTED_ZONES = _get_restricted_zones()
ALWAYS_VIOLATION_EVENTS = _get_always_violation_events()

# In-memory session tracker: (camera_id, zone_id) → datetime of first event (UTC naive)
_active_sessions: dict[tuple[str, str], datetime] = {}


def clear_session(camera_id: str, zone_id: str):
    """Clear session on zone exit."""
    _active_sessions.pop((camera_id, zone_id), None)


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

    # Session-based deduplication
    key = (event.camera_id, zone_id)
    now = datetime.utcnow()
    session_start = _active_sessions.get(key)

    if session_start is not None:
        elapsed = (now - session_start).total_seconds()
        if elapsed < settings.EVENT_STREAM_SUPPRESS_SECONDS:
            logger.debug(f"[UC5] Suppressed duplicate: {key} ({elapsed:.0f}s into session)")
            return
        if elapsed < settings.EVENT_STREAM_MAX_DURATION_SECONDS:
            logger.debug(f"[UC5] Suppressed duplicate: {key} ({elapsed:.0f}s into session)")
            return
        # Session expired — reset and treat as new event
        logger.info(f"[UC5] Session reset for {key} after {elapsed:.0f}s")
        del _active_sessions[key]

    # DB cooldown check (protects against restarts losing in-memory state)
    cooldown = timedelta(seconds=settings.VIOLATION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id,
        Alert.alert_type == "violation",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    if recent:
        return

    # First event in session — process and start tracking
    _active_sessions[key] = now
    desc = (f"Line crossing in zone {zone_id}" if event.event_type == "linedetection"
            else f"Vehicle in restricted zone: {zone_id}")
    logger.warning(f"[UC5] VIOLATION: {desc}")
    alert_zone_id = settings.CAMERA_ZONE_MAP.get(event.camera_id, zone_id)
    await create_alert(
        db,
        "violation",
        event.camera_id,
        alert_zone_id,
        event.event_type,
        desc,
        region_id=_parse_region_id(event.region_id),
        snapshot_path=event.snapshot_path,
    )


async def resolve_violation_on_exit(camera_id: str, zone_id: str, db: Session):
    """Auto-resolve the latest open violation when vehicle exits the restricted zone."""
    zone_ids = {zone_id}
    mapped = settings.CAMERA_ZONE_MAP.get(camera_id)
    if mapped:
        zone_ids.add(mapped)
    zone_ids = {z for z in zone_ids if z}
    if not zone_ids:
        return

    alert = (
        db.query(Alert)
        .filter(
            Alert.camera_id == camera_id,
            Alert.zone_id.in_(zone_ids),
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

