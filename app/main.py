# app/main.py
"""
FastAPI application entry point.
Includes security middleware, global error handlers, and all routers.
"""
# (no-op edit: 2026-05-05 to trigger uvicorn --reload — rev 3 for close_session vehicle clear)

import asyncio
import multiprocessing
import os
import time

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.concurrency import run_in_threadpool
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


async def _initialize_database_and_cameras() -> None:
    """Best-effort SQL bootstrap, kept off the API event loop."""
    try:
        await run_in_threadpool(create_tables)
        logger.info("✅ Database ready")
    except Exception as e:
        if "already an object named" in str(e):
            logger.info("✅ Database ready (schema already initialized by another worker)")
        else:
            logger.error(f"❌ Database initialization failed: {e}")

    # Load the camera inventory from the gateway-owned `cameras` table. Best
    # effort: on any failure the .env-built inventory (config.py) stays in place.
    from app.services.camera_loader import load_cameras_from_db

    await run_in_threadpool(load_cameras_from_db)

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
    durable_authoritative_intake = (
        settings.ENTRY_V2_MODE == "authoritative"
        and settings.CAMERA_INGEST_SPOOL_ENABLED
    )
    if durable_authoritative_intake:
        # A SQL outage must not keep the trusted write-ahead receipt endpoint
        # from starting. SQL/bootstrap work continues out of band; the replay
        # child owns its own DB connection and retries the durable queue.
        app.state.database_bootstrap = asyncio.create_task(
            _initialize_database_and_cameras(), name="database-bootstrap"
        )
    else:
        await _initialize_database_and_cameras()

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

    # Stage 1: durable intake owns authoritative camera input before processing.
    # Replay runs in a dedicated process so a synchronous pyodbc wait cannot
    # freeze this API loop and prevent later receipts.
    if settings.CAMERA_INGEST_SPOOL_ENABLED:
        from app.services.camera_ingest_spool import check_spool_durability
        from app.services.camera_ingest_spool import ReplayOwnerLease
        from app.services.camera_ingest_worker import CameraIngestWorker

        # Probe the configured path and durability syscalls. This does not
        # establish that deployment storage survives pod replacement.
        check_spool_durability()
        if durable_authoritative_intake:
            # A child owns volatile VMR state and forwards replayed SSE payloads
            # to this parent. Multiple API parents sharing one spool would split
            # both state channels, so reject the second owner before it accepts
            # any traffic. One async API process still handles many cameras.
            app.state.camera_ingest_replay_owner = ReplayOwnerLease.acquire()
            context = multiprocessing.get_context("spawn")
            app.state.camera_ingest_notification_stop = context.Event()
            app.state.camera_ingest_notification_queue = context.Queue(
                maxsize=settings.CAMERA_INGEST_NOTIFICATION_QUEUE_CAPACITY
            )
            app.state.camera_ingest_worker = CameraIngestWorker(
                worker_args=(app.state.camera_ingest_notification_queue,)
            )
            app.state.camera_ingest_worker.start()
            app.state.camera_ingest_supervisor = asyncio.create_task(
                _camera_ingest_worker_supervisor_loop()
            )
            app.state.camera_ingest_notification_bridge = asyncio.create_task(
                _camera_ingest_notification_bridge_loop()
            )
            logger.info("📥 Camera ingest-spool replay worker started")
        else:
            # Off/shadow retain the original in-process dispatcher so its
            # process-local SSE bus and burst/VMR coordination stay intact.
            app.state.camera_ingest_drainer = asyncio.create_task(
                _legacy_camera_ingest_drainer_loop()
            )
            app.state.camera_ingest_worker = None
            app.state.camera_ingest_supervisor = None
            app.state.camera_ingest_notification_bridge = None
            app.state.camera_ingest_replay_owner = None
    else:
        app.state.camera_ingest_drainer = None
        app.state.camera_ingest_worker = None
        app.state.camera_ingest_supervisor = None
        app.state.camera_ingest_notification_bridge = None
        app.state.camera_ingest_replay_owner = None

    # Heal whatever the gate pipeline missed while this service was down. The
    # event-driven reconcile only looks back 15 minutes, so without this a
    # restart after any real outage leaves those sessions open forever and the
    # dashboard reports the cars as overstays. Background and non-blocking: a
    # HikCentral that is unreachable at boot must not delay startup.
    if settings.HIK_CATCHUP_ON_STARTUP:
        from app.services.entry_exit_service import startup_catchup

        app.state.hik_catchup = asyncio.create_task(startup_catchup())
        logger.info("🔁 HikCentral restart catch-up task started")
    else:
        app.state.hik_catchup = None


async def _camera_ingest_worker_supervisor_loop() -> None:
    """Restart the authoritative replay child after an unexpected exit."""
    while True:
        try:
            await asyncio.sleep(1)
            worker = getattr(app.state, "camera_ingest_worker", None)
            if worker is not None:
                worker.ensure_running()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[IngestSpool] replay worker supervision failed")


async def _camera_ingest_notification_bridge_loop() -> None:
    """Relay replay-child notifications into this API process's SSE bus."""
    from app.services.camera_ingest_notifications import relay_child_notifications

    await relay_child_notifications(
        app.state.camera_ingest_notification_queue,
        app.state.camera_ingest_notification_stop,
    )


async def _legacy_camera_ingest_drainer_loop() -> None:
    """Preserve off/shadow replay inside the API process and its local bus."""
    from app.services.camera_ingest_worker import drain_camera_ingest_records_once

    while True:
        try:
            await asyncio.sleep(float(settings.CAMERA_INGEST_DRAIN_INTERVAL_SECONDS))
            await drain_camera_ingest_records_once()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("[IngestSpool] legacy drain tick failed")


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
    for attr in (
        "entry_burst_flusher", "pms_forward_drainer", "database_bootstrap",
        "camera_ingest_drainer", "camera_ingest_supervisor",
    ):
        task = getattr(app.state, attr, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    worker = getattr(app.state, "camera_ingest_worker", None)
    notification_stop = getattr(app.state, "camera_ingest_notification_stop", None)
    if notification_stop is not None:
        notification_stop.set()
    if worker is not None:
        await worker.stop()
    owner = getattr(app.state, "camera_ingest_replay_owner", None)
    if owner is not None:
        owner.release()
    bridge = getattr(app.state, "camera_ingest_notification_bridge", None)
    if bridge is not None:
        bridge.cancel()
        try:
            await bridge
        except asyncio.CancelledError:
            pass
    notification_queue = getattr(app.state, "camera_ingest_notification_queue", None)
    if notification_queue is not None:
        notification_queue.close()
        notification_queue.join_thread()
    await close_entry_v2_http_client()
    await close_core_backend_http_client()
    await close_hikcentral_http_client()
