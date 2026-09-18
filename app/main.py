# app/main.py
"""
FastAPI application entry point.
Includes security middleware, global error handlers, and all routers.
"""
# (no-op edit: 2026-05-05 to trigger uvicorn --reload — rev 3 for close_session vehicle clear)

import asyncio
import os
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

    # Stage 1: camera events that cannot be processed on arrival are spooled and
    # replayed instead of being answered with a 503 the camera will never retry.
    if settings.CAMERA_INGEST_SPOOL_ENABLED:
        from app.services.camera_ingest_spool import check_spool_durability

        # Reports whether the spool directory actually survives a restart. Never
        # raises — a config check that raises on an optional dependency is what
        # crash-looped this deployment once already.
        check_spool_durability()
        app.state.camera_ingest_drainer = asyncio.create_task(
            _camera_ingest_drainer_loop()
        )
        logger.info("📥 Camera ingest-spool drainer task started")
    else:
        app.state.camera_ingest_drainer = None

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


# Roughly every 5 minutes at the default 20s drain interval.
_STUCK_HEAD_LOG_EVERY = 15


async def _camera_ingest_drainer_loop():
    """Replay spooled camera events until they are accepted.

    Records are replayed oldest-first so an entry burst (CAM-23, CAM-03 and the
    ANPR read for one car, seconds apart) reaches the correlation logic in the
    order the cameras produced it. Each pass stops at the first record that is
    still retryable: a blocked downstream blocks the whole queue on purpose,
    because draining past it would reorder the burst.
    """
    from app.database import SessionLocal
    from app.routers.events import process_camera_event
    from app.services import camera_ingest_spool as spool

    while True:
        try:
            await asyncio.sleep(float(settings.CAMERA_INGEST_DRAIN_INTERVAL_SECONDS))
            for path in spool.iter_spooled_records():
                try:
                    header, raw_body = spool.read_record(path)
                except spool.SpoolRecordMalformed as exc:
                    spool.quarantine_record(path, str(exc))
                    continue
                except OSError:
                    break

                db = SessionLocal()
                try:
                    outcome = await process_camera_event(
                        raw_body,
                        str(header.get("camera_ip") or ""),
                        str(header.get("content_type") or ""),
                        db,
                    )
                finally:
                    db.close()

                if outcome.retryable:
                    # Count attempts for visibility only. Attempts must NOT decide
                    # quarantine: ordering means only the head is ever retried, so
                    # a downstream outage drives the head's counter up at a fixed
                    # rate and would quarantine a perfectly good event purely for
                    # having been first. At a 20s interval a 48-attempt cap
                    # discarded the oldest car after 16 minutes, and another every
                    # 16 minutes after that — roughly 97 cars across the 26-hour
                    # database outage this spool exists to survive.
                    #
                    # A genuinely poisonous record fails deterministically, which
                    # is a "rejected" outcome, not a retryable one, and is drained
                    # below. So the only bound needed here is age.
                    attempts = spool.record_attempt(path, header)
                    age = spool.record_age_seconds(header)
                    if age is not None and age >= settings.CAMERA_INGEST_SPOOL_MAX_AGE_SECONDS:
                        spool.quarantine_record(
                            path,
                            f"still retryable after {age / 3600:.1f}h and "
                            f"{attempts} attempts: {outcome.detail}",
                        )
                        continue
                    if attempts % _STUCK_HEAD_LOG_EVERY == 0:
                        logger.warning(
                            "[IngestSpool] head of queue blocked for %s attempts "
                            "(%.1f min, %d records waiting) — downstream still "
                            "failing: %s",
                            attempts,
                            (age or 0) / 60,
                            len(spool._record_paths(quarantine_unreadable=False)),
                            outcome.detail,
                        )
                    # Preserve ordering: leave this record and everything behind
                    # it for the next pass rather than replaying out of sequence.
                    break

                logger.info(
                    "[IngestSpool] replayed %s -> %s (spooled %s)",
                    os.path.basename(path),
                    outcome.status,
                    header.get("received_at"),
                )
                spool.remove_record(path)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[IngestSpool] drain tick failed: {e}", exc_info=True)


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
    for attr in ("entry_burst_flusher", "pms_forward_drainer", "camera_ingest_drainer"):
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
