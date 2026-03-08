# app/routers/entry_exit.py
"""UC1: Entry/Exit Counting endpoints."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models.entry_exit_log import EntryExitLog
from app.schemas.entry_exit_log import EntryExitLogResponse
from app.schemas.responses import TodayCountResponse
from datetime import date

router = APIRouter()


@router.get("/entry-exit", response_model=list[EntryExitLogResponse], summary="UC1 — Get entry/exit log")
def get_entry_exit_log(limit: int = 50, offset: int = 0, gate: str = None, db: Session = Depends(get_db)):
    """Returns chronological entry/exit events with plate, type, and time."""
    q = db.query(EntryExitLog)
    if gate:
        q = q.filter(EntryExitLog.gate == gate)
    return q.order_by(EntryExitLog.event_time.desc()).offset(offset).limit(limit).all()


@router.get("/entry-exit/count/today", response_model=TodayCountResponse, summary="UC1 — Today's vehicle count")
def get_today_counts(db: Session = Depends(get_db)):
    """Returns total entries and exits for today."""
    today = date.today()
    entries = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "entry",
        func.date(EntryExitLog.event_time) == today,
    ).scalar()
    exits = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "exit",
        func.date(EntryExitLog.event_time) == today,
    ).scalar()
    return {"date": str(today), "entries": entries, "exits": exits,
            "currently_parked": max(0, entries - exits)}
