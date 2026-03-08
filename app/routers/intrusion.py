<<<<<<< HEAD
# app/routers/intrusion.py
"""UC6: Intrusion Detection — list + resolve endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut

router = APIRouter()


@router.get("/intrusions", response_model=list[AlertOut], summary="UC6 — List intrusion alerts")
def get_intrusions(
    limit: int = 50,
    camera_id: str = None,
    zone_id: str = None,
    is_resolved: int = None,
    db: Session = Depends(get_db),
):
    """Returns intrusion alerts, newest first. Filter by camera_id, zone_id, or is_resolved (0/1)."""
    q = db.query(Alert).filter(Alert.alert_type == "intrusion")
    if camera_id:
        q = q.filter(Alert.camera_id == camera_id)
    if zone_id:
        q = q.filter(Alert.zone_id == zone_id)
    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)
    return q.order_by(Alert.triggered_at.desc()).limit(limit).all()


@router.put("/intrusions/resolve-all", summary="UC6 — Resolve all active intrusions")
def resolve_all_intrusions(db: Session = Depends(get_db)):
    """Mark all active intrusion alerts as resolved."""
    updated_count = db.query(Alert).filter(
        Alert.alert_type == "intrusion",
        Alert.is_resolved == 0
    ).update(
        {"is_resolved": 1, "resolved_at": datetime.utcnow()},
        synchronize_session=False
    )
    db.commit()
    return {"status": "resolved", "count": updated_count}


@router.put("/intrusions/{alert_id}/resolve", summary="UC6 — Resolve an intrusion alert")
def resolve_intrusion(alert_id: int, db: Session = Depends(get_db)):
    """Mark an intrusion alert as resolved."""
    alert = db.query(Alert).filter(Alert.id == alert_id, Alert.alert_type == "intrusion").first()
    if not alert:
        raise HTTPException(status_code=404, detail="Intrusion alert not found")
    alert.is_resolved = 1
    alert.resolved_at = datetime.utcnow()
    db.commit()
    return {"id": alert_id, "status": "resolved"}
=======
# app/routers/intrusion.py
"""UC6: Intrusion Detection — list alerts endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut

router = APIRouter()


@router.get("/intrusions", response_model=list[AlertOut], summary="UC6 — List intrusion alerts")
def get_intrusions(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    """Returns intrusion alerts, newest first."""
    return (
        db.query(Alert)
        .filter(Alert.alert_type == "intrusion")
        .order_by(Alert.triggered_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
>>>>>>> origin/Amr
