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
def get_violations(limit: int = 50, is_resolved: bool = None, db: Session = Depends(get_db)):
    """Returns violation alerts, newest first. Filter by is_resolved (true or false)."""
    q = db.query(Alert).filter(Alert.alert_type == "violation")
    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)
    return q.order_by(Alert.triggered_at.desc()).limit(limit).all()


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
