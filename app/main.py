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
    events, occupancy, violations, intrusion, 
    health, alerts, vehicles, entry_exit, parking_stats, camera_filter
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
            "/redoc", "/openapi.json", "/api/v1/alerts", "/api/v1/camera-filter"
        }
        open_prefixes = ("/api/v1/intrusions", "/api/v1/violations")
        
        if request.url.path in open_paths or request.url.path.startswith(open_prefixes) or not settings.API_KEY:
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
app.include_router(violations.router,    prefix="/api/v1", tags=["🚨 Violations — UC5"])
app.include_router(intrusion.router,     prefix="/api/v1", tags=["🔒 Intrusion — UC6"])
app.include_router(health.router,        prefix="/api/v1", tags=["💚 Health"])
app.include_router(alerts.router,        prefix="/api/v1", tags=["🔔 Alerts"])
app.include_router(camera_filter.router, prefix="/api/v1", tags=["📷 Camera Filter"])

# Phase 2 Routers (Active Now)
app.include_router(entry_exit.router,    prefix="/api/v1", tags=["🚗 Entry/Exit — UC1"])
app.include_router(parking_stats.router, prefix="/api/v1", tags=["📊 Stats — UC2"])
app.include_router(vehicles.router,      prefix="/api/v1", tags=["🔍 Vehicles — UC4"])

import asyncio
from app.services.occupancy_service import process_pending_exits
from app.database import SessionLocal

async def background_task_loop():
    """Periodically processes background tasks like pending occupancy exits."""
    while True:
        try:
            db = SessionLocal()
            try:
                await process_pending_exits(db)
                db.commit()
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Error in background task loop: {e}")
        await asyncio.sleep(2)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("🚀 Damanat Backend starting up...")
    try:
        create_tables()     
        logger.info("✅ Database ready")
    except Exception as e:
        logger.error(f"❌ Database initialization failed: {e}")

    # Start background task loop
    asyncio.create_task(background_task_loop())

    logger.info(f"📡 Cameras configured: {list(settings.CAMERAS.keys())}")
    logger.info(f"🌐 Listening on http://{settings.BACKEND_IP}:{settings.BACKEND_PORT}")

@app.on_event("shutdown")
async def shutdown():
    logger.info("🛑 Damanat Backend shutting down...")