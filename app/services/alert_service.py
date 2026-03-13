# app/services/alert_service.py
"""
Shared alert creation service.
Used by occupancy_service, violation_service, intrusion_service, and entry_exit_service.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.utils.logger import get_logger

logger = get_logger(__name__)

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
        new_alert = Alert(
            alert_type=alert_type,
            camera_id=camera_id,
            zone_id=zone_id,
            zone_name=zone_name if zone_name is not None else zone_id,
            region_id=region_id,
            slot_number=slot_number,
            event_type=event_type,
            description=description,
            snapshot_path=snapshot_path,
            is_resolved=False,
            triggered_at=datetime.utcnow(),
        )

        db.add(new_alert)
        logger.warning(
            f"[ALERT][{alert_type.upper()}] Cam: {camera_id} | Zone: {zone_name or zone_id} "
            f"| Region: {region_id} | Slot: {slot_number} | {description}"
        )

        # Future Expansion: Add push notifications or email triggers here

    except Exception as e:
        logger.error(f"Failed to create alert: {e}", exc_info=True)
        # We don't raise here to prevent the main event processing from failing
        # just because the alert logging had an issue.