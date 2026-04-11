"""Internal endpoints used by System 2 to enrich open parking sessions."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.parking_session import (
    ParkingSessionActionResponse,
    ParkingSessionBindRequest,
    ParkingSessionUnbindRequest,
)
from app.services import parking_session_service

router = APIRouter(prefix="/internal/parking-sessions", tags=["Internal Parking Sessions"])


@router.post("/bind-slot", response_model=ParkingSessionActionResponse)
def bind_slot(body: ParkingSessionBindRequest, db: Session = Depends(get_db)):
    try:
        session = parking_session_service.bind_slot(
            db,
            plate_number=body.plate_number,
            slot_number=body.slot_number,
            zone_id=body.zone_id,
            zone_name=body.zone_name,
            floor=body.floor,
            camera_id=body.camera_id,
            parked_at=body.parked_at,
            snapshot_path=body.snapshot_path,
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ParkingSessionActionResponse(
        session_id=session.id,
        plate_number=session.plate_number,
        status=session.status,
        zone_id=session.zone_id,
        zone_name=session.zone_name,
        floor=session.floor,
        slot_number=session.slot_number,
    )


@router.post("/unbind-slot", response_model=ParkingSessionActionResponse)
def unbind_slot(body: ParkingSessionUnbindRequest, db: Session = Depends(get_db)):
    try:
        session = parking_session_service.unbind_slot(
            db,
            plate_number=body.plate_number,
            camera_id=body.camera_id,
            left_at=body.left_at,
            snapshot_path=body.snapshot_path,
            slot_number=body.slot_number,
        )
        db.commit()
    except LookupError as exc:
        db.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return ParkingSessionActionResponse(
        session_id=session.id,
        plate_number=session.plate_number,
        status=session.status,
        zone_id=session.zone_id,
        zone_name=session.zone_name,
        floor=session.floor,
        slot_number=session.slot_number,
    )
