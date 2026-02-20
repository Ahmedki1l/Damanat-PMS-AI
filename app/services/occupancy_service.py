# app/services/occupancy_service.py
"""
UC3: Parking Occupancy Monitoring
Camera: CAM-03 (DS-2CD3783G2 AcuSense) only
Events: regionEntrance (+1), regionExiting (-1)
Camera config: Draw parking row zones on CAM-03 web UI with regionID labels
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.zone_occupancy import ZoneOccupancy
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def handle_occupancy_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or f"{event.camera_id}-default"
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        zone = ZoneOccupancy(zone_id=zone_id, camera_id=event.camera_id,
                             current_count=0, max_capacity=10, last_updated=datetime.utcnow())
        db.add(zone)

    if event.event_type == "regionEntrance":
        zone.current_count = max(0, zone.current_count + 1)
    elif event.event_type == "regionExiting":
        zone.current_count = max(0, zone.current_count - 1)

    zone.last_updated = datetime.utcnow()
    db.commit()
    logger.info(f"[UC3] {zone_id}: {zone.current_count}/{zone.max_capacity}")

    if zone.max_capacity and (zone.current_count / zone.max_capacity) >= settings.OCCUPANCY_ALERT_THRESHOLD:
        await create_alert(db, "occupancy_full", event.camera_id, zone_id, event.event_type,
                           f"Zone {zone_id} at {int(zone.current_count/zone.max_capacity*100)}% capacity")
