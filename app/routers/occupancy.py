# app/routers/occupancy.py
"""UC3: Parking Occupancy — read + capacity management endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, UTC
from app.config import settings, facility_now_naive
from app.database import get_db
from app.models.zone_occupancy import ZoneOccupancy
from app.schemas.zone_occupancy import ZoneOccupancyOut, ZoneCapacityUpdate
from app.schemas.responses import ZoneCapacityResponse, ZoneResetResponse

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


@router.put("/occupancy/{zone_id}/capacity", response_model=ZoneCapacityResponse, summary="Set max capacity for a zone")
def set_zone_capacity(zone_id: str, body: ZoneCapacityUpdate, db: Session = Depends(get_db)):
    """
    Update the maximum vehicle capacity for a zone.
    Call this once per zone during system setup.
    """
    zone_meta = settings.get_zone_metadata(zone_id)
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        zone = ZoneOccupancy(zone_id=zone_id, camera_id="manual",
                             zone_name=zone_meta.get("zone_name"),
                             floor=zone_meta.get("floor"),
                             current_count=0, max_capacity=body.max_capacity,
                             last_updated=facility_now_naive())
        db.add(zone)
    else:
        zone.max_capacity = body.max_capacity
        if zone_meta.get("zone_name") and not zone.zone_name:
            zone.zone_name = zone_meta["zone_name"]
        if zone_meta.get("floor") and not zone.floor:
            zone.floor = zone_meta["floor"]
    db.commit()
    return {"zone_id": zone_id, "max_capacity": body.max_capacity, "status": "updated"}


@router.put("/occupancy/{zone_id}/reset", response_model=ZoneResetResponse, summary="Reset zone count to zero")
def reset_zone_count(zone_id: str, db: Session = Depends(get_db)):
    """Manually reset zone vehicle count. Use after system restart or miscounts."""
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' not found")
    zone.current_count = 0
    zone.last_updated = facility_now_naive()
    db.commit()
    return {"zone_id": zone_id, "current_count": 0, "status": "reset"}

