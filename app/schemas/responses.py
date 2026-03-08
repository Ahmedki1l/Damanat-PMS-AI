# app/schemas/responses.py
"""Shared response schemas for endpoints that return simple status dicts."""
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class StatusResponse(BaseModel):
    status: str


class EventResponse(StatusResponse):
    event_type: Optional[str] = None
    reason: Optional[str] = None
    detail: Optional[str] = None


class ZoneCapacityResponse(StatusResponse):
    zone_id: str
    max_capacity: int


class ZoneResetResponse(StatusResponse):
    zone_id: str
    current_count: int


class ResolveResponse(BaseModel):
    id: int
    status: str


class VehicleActionResponse(StatusResponse):
    plate: str


class VehicleLookupResponse(BaseModel):
    plate: str
    status: str
    registered: bool
    owner: Optional[str] = None
    type: Optional[str] = None


class TodayCountResponse(BaseModel):
    date: str
    entries: int
    exits: int
    currently_parked: int


class ParkingTimeResponse(BaseModel):
    date: str
    avg_parking_minutes: float
    avg_parking_seconds: float


class DailyStatsResponse(BaseModel):
    date: str
    total_vehicles: int
    avg_parking_minutes: float


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    backend: str
    database: str
    cameras: dict[str, str]
