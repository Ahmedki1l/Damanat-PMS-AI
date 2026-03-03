# app/main.py
"""
FastAPI application entry point.
Includes security middleware, global error handlers, and all routers.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.routers import events, occupancy, violations, intrusion, health, alerts
from app.database import create_tables
from app.config import settings
from app.utils.logger import get_logger
import time
import asyncio

# Phase 2 routers — uncomment when ANPR cameras are installed
# from app.routers import entry_exit, parking_stats, vehicles

logger = get_logger(__name__)

app = FastAPI(
    title="Damanat Parking Analytics API",
    description="AI Camera event processing — Phase 1 + Phase 2. Fully offline.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS (allow dashboard on same LAN to call the API) ──────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Restrict to dashboard IP in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Key Middleware ───────────────────────────────────────────────────────
class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Optional lightweight API key auth for non-camera endpoints.
    Camera webhook (/api/v1/events/camera) is excluded — cameras don't send keys.
    Set API_KEY in .env. Leave empty to disable auth.
    """
    async def dispatch(self, request: Request, call_next):
        # Always allow camera webhook and health check without auth
        open_paths = {"/api/v1/events/camera", "/api/v1/health", "/docs", "/redoc", "/openapi.json", "/api/v1/alerts"}
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


# ── Request Timing Middleware ────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000, 2)
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


# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(events.router,     prefix="/api/v1", tags=["📡 Camera Events"])
app.include_router(occupancy.router,  prefix="/api/v1", tags=["🅿️  Occupancy — UC3"])
app.include_router(violations.router, prefix="/api/v1", tags=["🚨 Violations — UC5"])
app.include_router(intrusion.router,  prefix="/api/v1", tags=["🔒 Intrusion — UC6"])
app.include_router(health.router,     prefix="/api/v1", tags=["💚 Health"])
app.include_router(alerts.router,     prefix="/api/v1", tags=["🔔 Alerts"])

# Phase 2 — uncomment when ANPR cameras are installed
# app.include_router(entry_exit.router,    prefix="/api/v1", tags=["🚗 Entry/Exit — UC1"])
# app.include_router(parking_stats.router, prefix="/api/v1", tags=["📊 Stats — UC2"])
# app.include_router(vehicles.router,      prefix="/api/v1", tags=["🔍 Vehicles — UC4"])


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("🚀 Damanat Backend starting up...")
    create_tables()
    logger.info("✅ Database tables ready")
    logger.info(f"📡 Cameras configured: {list(settings.CAMERAS.keys())}")
    logger.info(f"🌐 Listening on http://{settings.BACKEND_IP}:{settings.BACKEND_PORT}")
    logger.info("📖 API docs at /docs")

    # Start pulling events from cameras via ISAPI alertStream
    # from app.services.camera_poller import start_camera_polling
    # asyncio.create_task(start_camera_polling(settings.CAMERAS))
    # logger.info("📡 Camera polling started (pull mode)")


@app.on_event("shutdown")
async def shutdown():
    logger.info("🛑 Damanat Backend shutting down...")
