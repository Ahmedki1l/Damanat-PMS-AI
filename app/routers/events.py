# app/routers/events.py
"""
Camera event webhook endpoint + raw event log viewer.
POST /events/camera — receives events from all cameras (XML or JSON).
GET  /events       — lists raw event log with optional filters.
"""

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from datetime import datetime
from app.database import get_db
from app.services.event_parser import parse_camera_event
from app.services.event_dispatcher import dispatch_event
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/events/camera", summary="Camera webhook — receives all events")
async def receive_camera_event(request: Request, db: Session = Depends(get_db)):
    """
    Single entry point for ALL camera events (Phase 1 + Phase 2).
    Always returns HTTP 200 — cameras retry on non-200 and we never want that.
    """
    try:
        raw_body = await request.body()
        if not raw_body:
            return {"status": "ignored", "reason": "empty body"}

        camera_ip = request.client.host
        content_type = request.headers.get("content-type", "")
        logger.info(f"Event from {camera_ip} | {len(raw_body)} bytes | {content_type}")

        # Parse into unified ParsedCameraEvent (handles XML and JSON)
        event = parse_camera_event(raw_body, camera_ip, content_type)
        logger.info(
            f"Parsed: type={event.event_type} state={event.event_state} "
            f"desc={event.event_description} target={event.detection_target} "
            f"zone={event.region_id} plate={event.plate_number} "
            f"snap={event.snapshot_path}"
        )

        # Persist raw event
        from app.models.camera_event import CameraEvent
        db.add(CameraEvent(
            camera_id=event.camera_id,
            device_serial=event.device_serial,
            channel_id=event.channel_id,
            event_type=event.event_type,
            event_state=event.event_state,
            event_description=event.event_description,
            detection_target=event.detection_target,
            region_id=event.region_id,
            channel_name=event.channel_name,
            trigger_time=event.trigger_time,
            snapshot_path=event.snapshot_path,
            raw_payload=event.raw_xml,
            created_at=datetime.utcnow(),
        ))
        db.commit()

        # Dispatch to correct use-case handlers
        await dispatch_event(event, db)
        return {"status": "ok", "event_type": event.event_type}

    except Exception as e:
        logger.error(f"Event processing error: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}  # Still return 200


@router.get("/events", summary="List raw camera events")
def list_events(limit: int = 50, camera_id: str = None, event_type: str = None,
                db: Session = Depends(get_db)):
    """Returns raw event log with optional camera_id and event_type filters."""
    from app.models.camera_event import CameraEvent
    q = db.query(CameraEvent)
    if camera_id:
        q = q.filter(CameraEvent.camera_id == camera_id)
    if event_type:
        q = q.filter(CameraEvent.event_type == event_type)
    return q.order_by(CameraEvent.created_at.desc()).limit(limit).all()

