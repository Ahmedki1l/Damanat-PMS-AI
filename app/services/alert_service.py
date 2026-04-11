# app/services/alert_service.py
"""
Shared alert creation service.
Used by occupancy_service, violation_service, intrusion_service, and entry_exit_service.
"""

from datetime import datetime, UTC
from sqlalchemy.orm import Session
from app.config import settings
from app.models.alert import Alert
from app.utils.logger import get_logger
from app.utils.event_bus import event_bus

logger = get_logger(__name__)


ALERT_SEVERITY_MAP = {
    "unknown_vehicle": "critical",
    "violence": "critical",
    "intrusion": "critical",
    "vehicle_intrusion": "critical",
    "named_slot_violation": "critical",
    "vehicle_violation": "critical",
    "capacity_exceeded": "warning",
}


def _resolve_severity(alert_type: str) -> str:
    return ALERT_SEVERITY_MAP.get(alert_type, "warning")


def _resolve_location_display(
    camera_id: str,
    zone_id: str | None,
    zone_name: str | None,
    slot_number: int | str | None,
) -> str:
    if slot_number:
        return f"Slot {slot_number}"
    if zone_name:
        return zone_name
    if zone_id:
        zone_meta = settings.get_zone_metadata(zone_id)
        if zone_meta.get("zone_name"):
            return str(zone_meta["zone_name"])
        return zone_id

    camera = settings.CAMERAS.get(camera_id, {})
    if camera.get("name"):
        return str(camera["name"])

    gate = camera.get("gate")
    if gate == "entry":
        return "Entry Gate"
    if gate == "exit":
        return "Exit Gate"

    return camera_id


async def create_alert(
    db: Session,
    alert_type: str,
    camera_id: str,
    zone_id: str,
    event_type: str,
    description: str,
    snapshot_path: str | None = None,
    region_id: int | None = None,
    slot_number: int | None = None,
    zone_name: str | None = None,
    plate_number: str | None = None,
):
    """
    Create an alert record in the database.

    Parameters:
        zone_id:   canonical zone name used for cooldown/resolution logic (e.g. "restricted-vip")
        zone_name: human-readable stored name; defaults to zone_id if not provided
        region_id: raw integer region ID sent by the camera
        slot_number: real-life parking bay number (from ZONE_REAL_SLOT)
        snapshot_path: CDN URL or local file path for the evidence image.

    Note: The caller (event_dispatcher) is responsible for committing the transaction.
    """
    try:
        severity = _resolve_severity(alert_type)
        zone_meta = settings.get_zone_metadata(zone_id)
        resolved_zone_name = zone_name if zone_name is not None else zone_meta.get("zone_name") or zone_id
        location_display = _resolve_location_display(
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=resolved_zone_name,
            slot_number=slot_number,
        )
        new_alert = Alert(
            alert_type=alert_type,
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=resolved_zone_name,
            region_id=region_id,
            slot_number=slot_number,
            event_type=event_type,
            description=description,
            snapshot_path=snapshot_path,
            plate_number=plate_number,
            severity=severity,
            location_display=location_display,
            is_resolved=False,
            triggered_at=datetime.now(UTC),
        )

        db.add(new_alert)
        logger.warning(
            f"[ALERT][{alert_type.upper()}] Cam: {camera_id} | Zone: {zone_name or zone_id} "
            f"| Region: {region_id} | Slot: {slot_number} | {description}"
        )

        import json
        event_bus.publish(json.dumps({
            "is_alert": True,
            "severity": severity,
            "alert_type": alert_type,
            "camera_id": camera_id,
            "zone_id": zone_id,
            "zone_name": resolved_zone_name,
            "region_id": region_id,
            "slot_number": slot_number,
            "plate_number": plate_number,
            "location_display": location_display,
            "event_type": event_type,
            "description": description,
            "snapshot_path": snapshot_path,
            "triggered_at": new_alert.triggered_at.isoformat(),
        }))

    except Exception as e:
        logger.error(f"Failed to create alert: {e}", exc_info=True)
        # We don't raise here to prevent the main event processing from failing
        # just because the alert logging had an issue.
