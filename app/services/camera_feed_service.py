from datetime import datetime, UTC
from sqlalchemy.orm import Session
from app.models.camera_feed import CameraFeed
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

def add_event_to_feed(db: Session, event):
    """
    Log a camera event to the CameraFeed table for real-time display.
    'event' is expected to be a ParsedCameraEvent (dataclass).
    """
    try:
        # ── FILTERING LOGIC ──────────────────────────────────────────────────
        # 1. ANPR / Vehicle Match: Include ONLY for entry and exit gates.
        # 2. Line Detection: Include for ALL cameras.
        
        cam_config = settings.CAMERAS.get(event.camera_id, {})
        gate = cam_config.get("gate", event.gate)
        
        is_anpr_type = event.event_type in ("ANPR", "vehicleMatchResult", "AccessControllerEvent")
        is_line_detection = event.event_type == "linedetection"
        
        if is_anpr_type and not gate:
            # Skip ANPR events that are not from a designated gate
            logger.debug(f"[CameraFeed] Filtered out non-gate ANPR event from {event.camera_id}")
            return
            
        if not is_anpr_type and not is_line_detection:
            # Skip any other smart event types (fielddetection, regionEntrance, etc. if not requested)
            logger.debug(f"[CameraFeed] Filtered out event type {event.event_type} from {event.camera_id}")
            return

        # ── MAPPING & NORMALIZATION ──────────────────────────────────────────
        source_map = {
            "linedetection": "Line Detection",
            "ANPR": "ANPR",
            "vehicleMatchResult": "Vehicle Match",
            "AccessControllerEvent": "Vehicle Match",
        }
        detection_source = source_map.get(event.event_type, event.event_type.replace("_", " ").title())

        # Derive Location Label (e.g. "(B1-EXIT)")
        location_raw = cam_config.get("name", event.camera_id)
        if gate:
            floor = "B1"
            if "B2" in location_raw or "B2" in event.camera_id:
                floor = "B2"
            elif "GF" in location_raw or "ENTRY" in event.camera_id or "EXIT" in event.camera_id:
                floor = "GF"
            location_label = f"({floor}-{gate.upper()})"
        else:
            location_label = f"({location_raw})"

        # 3. Create the Database Record
        feed_entry = CameraFeed(
            camera_id=event.camera_id,
            location_label=location_label,
            event_description=event.event_description or "Vehicle detected",
            detection_source=detection_source,
            plate_number=getattr(event, "plate_number", None),
            snapshot_path=event.snapshot_path,
            timestamp=event.trigger_time or datetime.now(UTC)
        )

        db.add(feed_entry)
        logger.debug(f"[CameraFeed] Logged {event.camera_id} {location_label} via {detection_source}")

    except Exception as e:
        logger.error(f"Failed to log to CameraFeed: {e}", exc_info=True)
