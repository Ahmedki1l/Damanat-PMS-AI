# app/routers/alerts.py

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut
from app.utils.event_bus import event_bus
from sse_starlette.sse import EventSourceResponse
from typing import Optional
import json
from app.services.alert_service import broadcast_event

router = APIRouter()


@router.get(
    "/alerts/stream",
    summary="Real-time alert stream (SSE)",
    response_class=EventSourceResponse
)
async def stream_alerts():
    async def event_generator():
        async for event in event_bus.subscribe():
            # 👇 مهم: format SSE
            yield {
                "event": "alert",
                "data": event
            }

    return EventSourceResponse(event_generator())


@router.get("/alerts", response_model=list[AlertOut], summary="All alerts — filterable by type")
def get_all_alerts(
    alert_type: Optional[str] = None,
    is_resolved: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    q = db.query(Alert)

    if alert_type:
        q = q.filter(Alert.alert_type == alert_type)

    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)

    return q.order_by(Alert.triggered_at.desc()).offset(offset).limit(limit).all()


@router.post("/test-alert")
async def test_alert():
    """
    Triggers a dummy broadcast event using the standardized service.
    This ensures the payload matches the production schema.
    """
    event_data = {
        "is_alert": True,
        "severity": "critical",
        "event_type": "test_broadcast",
        "description": "REAL SSE TEST - Standardized Payload",
        "camera_id": "CAM-TEST",
        "alert_type": "intrusion",
        "alert_id": 999,
        "plate_number": "TEST-123",
        "snapshot_path": "https://dummy-cdn.com/test.jpg",
        "zone_id": "zone-test",
        "zone_name": "Test Zone",
        "slot_number": 101
    }

    await broadcast_event(**event_data)

    return {"status": "sent", "event": event_data}
