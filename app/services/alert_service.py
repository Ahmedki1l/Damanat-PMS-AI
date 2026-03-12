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
):
    """
    Create an alert record in the database.
    snapshot_path: CDN URL or local file path for the evidence image.
    Note: The caller (event_dispatcher) is responsible for committing the transaction.
    """
    try:
        new_alert = Alert(
            alert_type=alert_type,
            camera_id=camera_id,
            zone_id=zone_id,
            region_id=region_id,
            event_type=event_type,
            description=description,
            snapshot_path=snapshot_path,
            is_resolved=0,
            triggered_at=datetime.utcnow(),
        )

        db.add(new_alert)
        logger.warning(f"[ALERT][{alert_type.upper()}] Cam: {camera_id} | Zone: {zone_id} | {description}")

        # Future Expansion: Add push notifications or email triggers here

    except Exception as e:
        logger.error(f"Failed to create alert: {e}", exc_info=True)
        # We don't raise here to prevent the main event processing from failing
        # just because the alert logging had an issue.