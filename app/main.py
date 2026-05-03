# app/main.py
"""
FastAPI application entry point.
Includes security middleware, global error handlers, and all routers.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.routers import (
    events, occupancy,
    health, alerts, vehicles, entry_exit, parking_stats, parking_sessions_internal,
    snapshots,
)
from app.database import create_tables
from app.config import settings
from app.utils.logger import get_logger
import time

logger = get_logger(__name__)

app = FastAPI(
    title="Damanat Parking Analytics API",
    description="AI Camera event processing — Phase 1 + Phase 2. Fully offline.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS ──────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── API Key Middleware ───────────────────────────────────────────────────────
class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        open_paths = {
            "/api/v1/events/camera", "/api/v1/health", "/docs", 
            "/redoc", "/openapi.json", "/api/v1/alerts"
        }
        
        if request.url.path in open_paths or not settings.API_KEY:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if api_key != settings.API_KEY:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)

if settings.API_KEY:
    app.add_middleware(APIKeyMiddleware)

# ── Request Timing & Logging Middleware ──────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000, 2)

    if request.url.path == "/api/v1/events/camera":
        client_ip = request.client.host if request.client else ""
        if client_ip not in settings.CAMERA_IP_MAP:
            return response

    logger.debug(f"{request.method} {request.url.path} → {response.status_code} ({duration}ms)")
    return response

# ── Global Exception Handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(events.router,        prefix="/api/v1", tags=["📡 Camera Events"])
app.include_router(occupancy.router,     prefix="/api/v1", tags=["🅿️ Occupancy — UC3"])
app.include_router(health.router,        prefix="/api/v1", tags=["💚 Health"])
app.include_router(alerts.router,        prefix="/api/v1", tags=["🔔 Alerts"])

# Phase 2 Routers (Active Now)
app.include_router(entry_exit.router,    prefix="/api/v1", tags=["🚗 Entry/Exit — UC1"])
app.include_router(parking_stats.router, prefix="/api/v1", tags=["📊 Stats — UC2"])
app.include_router(vehicles.router,      prefix="/api/v1", tags=["🔍 Vehicles — UC4"])

app.include_router(parking_sessions_internal.router, prefix="/api/v1", tags=["Internal Sessions"])

app.include_router(snapshots.router, tags=["📸 Snapshots"])

import asyncio
from app.database import SessionLocal


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("🚀 Damanat Backend starting up...")
    try:
        create_tables()     
        logger.info("✅ Database ready")
    except Exception as e:
        if "already an object named" in str(e):
            logger.info("✅ Database ready (schema already initialized by another worker)")
        else:
            logger.error(f"❌ Database initialization failed: {e}")



    logger.info(f"📡 Cameras configured: {list(settings.CAMERAS.keys())}")
    logger.info(f"🌐 Listening on http://{settings.BACKEND_IP}:{settings.BACKEND_PORT}")

@app.on_event("shutdown")
async def shutdown():
    logger.info("🛑 Damanat Backend shutting down...")
