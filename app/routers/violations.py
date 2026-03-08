# app/routers/violations.py
"""UC5: Proactive Violation Alerts — list + resolve endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut
from app.schemas.responses import ResolveResponse

router = APIRouter()


@router.get("/violations", response_model=list[AlertOut], summary="UC5 — List violation alerts")
def get_violations(
    limit: int = 50,
    offset: int = 0,
    camera_id: str = None,
    zone_id: str = None,
    is_resolved: bool = None,
    db: Session = Depends(get_db),
):
    """Returns violation alerts, newest first. Filter by camera_id, zone_id, or is_resolved."""
    q = db.query(Alert).filter(Alert.alert_type == "violation")
    if camera_id:
        q = q.filter(Alert.camera_id == camera_id)
    if zone_id:
        q = q.filter(Alert.zone_id == zone_id)
    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)
    return q.order_by(Alert.triggered_at.desc()).offset(offset).limit(limit).all()


@router.put("/violations/{alert_id}/resolve", response_model=ResolveResponse, summary="UC5 — Resolve a violation")
def resolve_violation(alert_id: int, db: Session = Depends(get_db)):
    """Mark a violation alert as resolved."""
    alert = db.query(Alert).filter(Alert.id == alert_id, Alert.alert_type == "violation").first()
    if not alert:
        raise HTTPException(status_code=404, detail="Violation not found")
    alert.is_resolved = True
    alert.resolved_at = datetime.utcnow()
    db.commit()
    return {"id": alert_id, "status": "resolved"}


@router.put("/violations/resolve-all", summary="UC5 — Resolve all active violations")
def resolve_all_violations(db: Session = Depends(get_db)):
    """Mark all active violation alerts as resolved."""
    updated_count = db.query(Alert).filter(
        Alert.alert_type == "violation",
        Alert.is_resolved == False,  # noqa: E712
    ).update(
        {"is_resolved": True, "resolved_at": datetime.utcnow()},
        synchronize_session=False,
    )
    db.commit()
    return {"status": "resolved", "count": updated_count}
