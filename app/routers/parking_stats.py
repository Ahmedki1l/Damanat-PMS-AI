# app/routers/parking_stats.py
"""UC2: Average Parking Time & Daily Vehicle Count (Phase 2)"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models.entry_exit_log import EntryExitLog
from app.schemas.responses import ParkingTimeResponse, DailyStatsResponse
from app.config import facility_day_range_utc, facility_today_utc

router = APIRouter()


@router.get("/stats/parking-time", response_model=ParkingTimeResponse, summary="UC2 — Average parking duration")
def get_avg_parking_time(target_date: str = None, db: Session = Depends(get_db)):
    """
    Returns average parking duration in minutes for a given facility-local date.
    Only includes vehicles with matched entry+exit pairs. The date window is
    facility-local (FACILITY_TIMEZONE_OFFSET_HOURS) so it matches the Gateway's
    `/entry-exit/kpis.avg_stay_minutes` tile.
    """
    start_utc, end_utc = facility_day_range_utc(target_date)
    avg_seconds = db.query(func.avg(EntryExitLog.parking_duration)).filter(
        EntryExitLog.gate == "exit",
        EntryExitLog.parking_duration != None,
        EntryExitLog.event_time >= start_utc,
        EntryExitLog.event_time < end_utc,
    ).scalar() or 0
    return {
        "date": target_date or start_utc.date().isoformat(),
        "avg_parking_minutes": round(avg_seconds / 60, 1),
        "avg_parking_seconds": round(avg_seconds, 0),
    }


@router.get("/stats/daily", response_model=DailyStatsResponse, summary="UC2 — Daily vehicle count summary")
def get_daily_stats(target_date: str = None, db: Session = Depends(get_db)):
    """Returns total vehicles in/out and average parking time per facility-local day."""
    start_utc, end_utc = facility_day_range_utc(target_date)
    target = target_date or start_utc.date().isoformat()
    total = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "entry",
        EntryExitLog.event_time >= start_utc,
        EntryExitLog.event_time < end_utc,
    ).scalar()
    avg_dur = db.query(func.avg(EntryExitLog.parking_duration)).filter(
        EntryExitLog.gate == "exit",
        EntryExitLog.parking_duration != None,
        EntryExitLog.event_time >= start_utc,
        EntryExitLog.event_time < end_utc,
    ).scalar() or 0
    return {"date": target, "total_vehicles": total,
            "avg_parking_minutes": round(avg_dur / 60, 1)}
