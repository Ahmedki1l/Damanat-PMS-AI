# app/routers/occupancy.py
"""UC3: Parking Occupancy — read + capacity management endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from app.database import get_db
from app.models.zone_occupancy import ZoneOccupancy
from app.schemas.zone_occupancy import ZoneOccupancyOut, ZoneCapacityUpdate

router = APIRouter()


@router.get("/occupancy", response_model=list[ZoneOccupancyOut])
def get_all_occupancy(db: Session = Depends(get_db)):
    """Current vehicle count for all zones."""
    zones = db.query(ZoneOccupancy).all()
    for z in zones:
        z.occupancy_percent = round((z.current_count / z.max_capacity) * 100, 1) if z.max_capacity else 0
        z.is_full = z.current_count >= z.max_capacity
    return zones


@router.get("/occupancy/{zone_id}", response_model=ZoneOccupancyOut)
def get_zone_occupancy(zone_id: str, db: Session = Depends(get_db)):
    """Occupancy for a specific zone."""
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' not found")
    zone.occupancy_percent = round((zone.current_count / zone.max_capacity) * 100, 1) if zone.max_capacity else 0
    zone.is_full = zone.current_count >= zone.max_capacity
    return zone


@router.put("/occupancy/{zone_id}/capacity", summary="Set max capacity for a zone")
def set_zone_capacity(zone_id: str, body: ZoneCapacityUpdate, db: Session = Depends(get_db)):
    """
    Update the maximum vehicle capacity for a zone.
    Call this once per zone during system setup.
    """
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        zone = ZoneOccupancy(zone_id=zone_id, camera_id="manual",
                             current_count=0, max_capacity=body.max_capacity,
                             last_updated=datetime.utcnow())
        db.add(zone)
    else:
        zone.max_capacity = body.max_capacity
    db.commit()
    return {"zone_id": zone_id, "max_capacity": body.max_capacity, "status": "updated"}


@router.put("/occupancy/{zone_id}/reset", summary="Reset zone count to zero")
def reset_zone_count(zone_id: str, db: Session = Depends(get_db)):
    """Manually reset zone vehicle count. Use after system restart or miscounts."""
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' not found")
    zone.current_count = 0
    zone.last_updated = datetime.utcnow()
    db.commit()
    return {"zone_id": zone_id, "current_count": 0, "status": "reset"}
