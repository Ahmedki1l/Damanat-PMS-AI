# app/routers/parking_stats.py
"""UC2: Average Parking Time & Daily Vehicle Count (Phase 2)"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models.entry_exit_log import EntryExitLog
from datetime import date

router = APIRouter()


@router.get("/stats/parking-time", summary="UC2 — Average parking duration")
def get_avg_parking_time(target_date: str = None, db: Session = Depends(get_db)):
    """
    Returns average parking duration in minutes for a given date.
    Only includes vehicles with matched entry+exit pairs.
    """
    q = db.query(func.avg(EntryExitLog.parking_duration)).filter(
        EntryExitLog.gate == "exit",
        EntryExitLog.parking_duration != None,
    )
    if target_date:
        q = q.filter(func.date(EntryExitLog.event_time) == target_date)

    avg_seconds = q.scalar() or 0
    return {
        "date": target_date or str(date.today()),
        "avg_parking_minutes": round(avg_seconds / 60, 1),
        "avg_parking_seconds": round(avg_seconds, 0),
    }


@router.get("/stats/daily", summary="UC2 — Daily vehicle count summary")
def get_daily_stats(target_date: str = None, db: Session = Depends(get_db)):
    """Returns total vehicles in/out and average parking time per day."""
    target = target_date or str(date.today())
    total = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "entry",
        func.date(EntryExitLog.event_time) == target,
    ).scalar()
    avg_dur = db.query(func.avg(EntryExitLog.parking_duration)).filter(
        EntryExitLog.gate == "exit",
        EntryExitLog.parking_duration != None,
        func.date(EntryExitLog.event_time) == target,
    ).scalar() or 0
    return {"date": target, "total_vehicles": total,
            "avg_parking_minutes": round(avg_dur / 60, 1)}
