# app/main.py
"""
FastAPI application entry point.
Includes security middleware, global error handlers, and all routers.
"""
# (no-op edit: 2026-05-05 to trigger uvicorn --reload — rev 3 for close_session vehicle clear)

import asyncio
import time

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.routers import (
    events, occupancy,
    health, alerts, vehicles, entry_exit, parking_stats, parking_sessions_internal,
    entry_confirmations,
    slot_recoveries,
    snapshots,
)
from app.database import SessionLocal, create_tables, engine
from app.config import settings
from app.services.entry_state_lock import assert_authoritative_lock_backend
from app.services.entry_v2_forwarder import (
    close_entry_v2_http_client,
    start_entry_v2_http_client,
    start_entry_v2_shadow_worker,
    stop_entry_v2_shadow_worker,
)
from app.services.hikcentral import (
    close_hikcentral_http_client,
    start_hikcentral_http_client,
)
from app.utils.core_backend_client import (
    close_core_backend_http_client,
    start_core_backend_http_client,
)
from app.utils.logger import get_logger

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
            "/api/v1/internal/entry-confirmations",
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
app.include_router(entry_confirmations.router, prefix="/api/v1", tags=["Entry V2"])
app.include_router(slot_recoveries.router, prefix="/api/v1", tags=["Slot Recovery"])

app.include_router(snapshots.router, tags=["📸 Snapshots"])

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("🚀 Damanat Backend starting up...")
    # Authoritative entry/session writes require SQL Server's transaction-owned
    # application lock. This assertion is intentionally outside the best-effort
    # schema initialization block: continuing on another dialect would permit
    # concurrent duplicate/open-session races.
    assert_authoritative_lock_backend(engine.dialect.name)
    try:
        create_tables()
        logger.info("✅ Database ready")
    except Exception as e:
        if "already an object named" in str(e):
            logger.info("✅ Database ready (schema already initialized by another worker)")
        else:
            logger.error(f"❌ Database initialization failed: {e}")

    # Load the camera inventory from the gateway-owned `cameras` table. Best
    # effort: on any failure the .env-built inventory (config.py) stays in place.
    from app.services.camera_loader import load_cameras_from_db
    load_cameras_from_db()

    logger.info(f"📡 Cameras configured: {list(settings.CAMERAS.keys())}")
    logger.info(
        f"🕐 Facility TZ offset: UTC+{settings.FACILITY_TIMEZONE_OFFSET_HOURS} "
        "(env FACILITY_TIMEZONE_OFFSET_HOURS). Gateway must run with the same value."
    )
    logger.info(f"🌐 Listening on http://{settings.BACKEND_IP}:{settings.BACKEND_PORT}")

    await start_entry_v2_http_client()
    await start_entry_v2_shadow_worker()
    await start_core_backend_http_client()
    await start_hikcentral_http_client()

    # Background flusher for the ANPR entry-burst buffer. The CAM-23 ramp-top
    # crossing CONFIRMS each car, but the correct plate read lands 1-3s after the
    # crossing (recognition lag), so this task is what actually writes the entry:
    # once the burst goes idle (the lagging correct read is in) AND it has been
    # confirmed, it commits one entry on the winning plate and forwards it to the
    # PMS. It also drops never-confirmed ghosts at the hard cap and reaps silent
    # ramp crossings.
    if settings.ENTRY_V2_MODE != "authoritative":
        app.state.entry_burst_flusher = asyncio.create_task(_entry_burst_flusher_loop())
        logger.info("🧹 Entry-burst flusher task started")
    else:
        app.state.entry_burst_flusher = None
        logger.info("🧠 Entry V2 authoritative — legacy burst/FIFO flusher disabled")

    # Background drain for undelivered legacy ANPR forwards to the VA backend.
    # Authoritative Entry V2 quarantines old entry files; legacy exit payloads
    # remain eligible for delivery.
    app.state.pms_forward_drainer = asyncio.create_task(_pms_forward_drainer_loop())
    logger.info("📤 PMS/VA forward-spool drainer task started")


async def _pms_forward_drainer_loop():
    """Periodically re-POST any spooled ANPR forwards to the VA backend so an
    image is not lost when VA was down at forward time."""
    from app.utils.core_backend_client import drain_pms_forward_spool
    while True:
        try:
            await asyncio.sleep(float(settings.PMS_FORWARD_DRAIN_INTERVAL_SECONDS))
            await drain_pms_forward_spool()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[PMS] Forward-spool drain tick failed: {e}", exc_info=True)


async def _entry_burst_flusher_loop():
    """Every ~0.5s flush any confirmed entry burst that has settled (idle window)
    and reap silent ramp crossings. Uses its own DB session per tick."""
    from app.services.entry_exit_service import flush_due_entry_bursts
    while True:
        try:
            await asyncio.sleep(0.5)
            db = SessionLocal()
            try:
                await flush_due_entry_bursts(db)
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[UC1] Entry-burst flusher tick failed: {e}", exc_info=True)


@app.on_event("shutdown")
async def shutdown():
    logger.info("🛑 Damanat Backend shutting down...")
    # Stop accepting shadow observations, drain for a bounded interval, and
    # release queued image bytes before closing the shared VA HTTP client.
    await stop_entry_v2_shadow_worker()
    from app.services.entry_exit_service import drain_background_forwards
    await drain_background_forwards()
    for attr in ("entry_burst_flusher", "pms_forward_drainer"):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await close_entry_v2_http_client()
    await close_core_backend_http_client()
    await close_hikcentral_http_client()
