# app/routers/intrusion.py
"""UC6: Intrusion Detection — list alerts endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut

router = APIRouter()


@router.get("/intrusions", response_model=list[AlertOut], summary="UC6 — List intrusion alerts")
def get_intrusions(limit: int = 50, db: Session = Depends(get_db)):
    """Returns intrusion alerts, newest first."""
    return (
        db.query(Alert)
        .filter(Alert.alert_type == "intrusion")
        .order_by(Alert.triggered_at.desc())
        .limit(limit)
        .all()
    )
