# app/routers/alerts.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut
from app.utils.event_bus import event_bus
from sse_starlette.sse import EventSourceResponse
from typing import Optional

router = APIRouter()

@router.get(
    "/alerts/stream", 
    summary="Real-time alert stream (SSE)",
    response_class=EventSourceResponse
)
async def stream_alerts():
    """
    Server-Sent Events endpoint that pushes new alerts to the client.
    Usage: const source = new EventSource('/api/v1/alerts/stream');
    """
    return EventSourceResponse(event_bus.subscribe())

@router.get("/alerts", response_model=list[AlertOut], summary="All alerts — filterable by type")
def get_all_alerts(
    alert_type: Optional[str] = None,
    is_resolved: Optional[bool] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Combined alerts endpoint. Filter by alert_type or is_resolved."""
    q = db.query(Alert)
    if alert_type:
        q = q.filter(Alert.alert_type == alert_type)
    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)
    return q.order_by(Alert.triggered_at.desc()).offset(offset).limit(limit).all()