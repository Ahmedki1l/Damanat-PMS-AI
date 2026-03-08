# app/services/alert_service.py
"""
Shared alert creation service.
Used by occupancy_service, violation_service, intrusion_service, and entry_exit_service.
Extend here to add push notifications, SMS, email, etc.
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def create_alert(db, alert_type, camera_id, zone_id, event_type, description):
    """Create an alert record. Caller is responsible for committing the transaction."""
    db.add(Alert(alert_type=alert_type, camera_id=camera_id, zone_id=zone_id,
                 event_type=event_type, description=description,
                 is_resolved=0, triggered_at=datetime.utcnow()))
    logger.warning(f"[ALERT][{alert_type.upper()}] {description}")
