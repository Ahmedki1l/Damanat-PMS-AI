# app/routers/intrusion.py
"""
UC6: Intrusion Detection — Endpoints for listing and resolving alerts.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
from app.database import get_db
from app.models.alert import Alert
from app.schemas.alert import AlertOut

router = APIRouter()


@router.get("/intrusions", response_model=List[AlertOut], summary="UC6 — List intrusion alerts")
def get_intrusions(
    limit: int = 50,
    offset: int = 0,
    camera_id: Optional[str] = None,
    zone_id: Optional[str] = None,
    is_resolved: Optional[bool] = None,
    db: Session = Depends(get_db),
):
    """
    Returns intrusion alerts, newest first. 
    Supports filtering by camera, zone, and resolution status.
    """
    q = db.query(Alert).filter(Alert.alert_type == "intrusion")
    
    if camera_id:
        q = q.filter(Alert.camera_id == camera_id)
    if zone_id:
        q = q.filter(Alert.zone_id == zone_id)
    if is_resolved is not None:
        q = q.filter(Alert.is_resolved == is_resolved)
        
    return q.order_by(Alert.triggered_at.desc()).offset(offset).limit(limit).all()


@router.put("/intrusions/resolve-all", summary="UC6 — Resolve all active intrusions")
def resolve_all_intrusions(db: Session = Depends(get_db)):
    """
    Mark all active intrusion alerts as resolved. 
    Used for bulk clearing the dashboard.
    """
    updated_count = db.query(Alert).filter(
        Alert.alert_type == "intrusion",
        Alert.is_resolved == False
    ).update(
        {"is_resolved": True, "resolved_at": datetime.utcnow()},
        synchronize_session=False
    )
    db.commit()
    return {"status": "resolved", "count": updated_count}


@router.put("/intrusions/{alert_id}/resolve", summary="UC6 — Resolve an intrusion alert")
def resolve_intrusion(alert_id: int, db: Session = Depends(get_db)):
    """
    Mark a specific intrusion alert as resolved by its ID.
    """
    alert = db.query(Alert).filter(
        Alert.id == alert_id, 
        Alert.alert_type == "intrusion"
    ).first()
    
    if not alert:
        raise HTTPException(status_code=404, detail="Intrusion alert not found")
        
    alert.is_resolved = True
    alert.resolved_at = datetime.utcnow()
    db.commit()
    return {"id": alert_id, "status": "resolved"}