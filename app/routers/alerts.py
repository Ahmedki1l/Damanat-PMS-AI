# app/routers/alerts.py
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut
from typing import Optional

router = APIRouter()

@router.get("/alerts", response_model=list[AlertOut], summary="All alerts — filterable by type")
def get_all_alerts(
    alert_type: Optional[str] = None,
    is_resolved: Optional[int] = None,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """Combined alerts endpoint. Filter by alert_type or is_resolved."""
    q = db.query(Alert)
    if alert_type:
        q = q.filter(Alert.alert_type == alert_type)
    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)
    return q.order_by(Alert.triggered_at.desc()).limit(limit).all()