"""Dedicated process for replaying durable camera intake records.

The API process must be able to acknowledge a trusted camera after its record
has reached the spool even while SQL Server is blocked.  ``process_camera_event``
contains synchronous SQLAlchemy/pyodbc work alongside loop-affine async HTTP
and state coordination, so it cannot safely run in a thread owned by the API
event loop.  This module runs it in a spawned process with a fresh event loop
and fresh clients instead.
"""

import asyncio
import multiprocessing
import os
from typing import Callable, Optional

from app.config import settings
from app.database import SessionLocal
from app.routers.events import CameraEventOutcome, process_camera_event
from app.services import camera_ingest_spool as spool
from app.services.event_parser import CameraPayloadRejected, parse_camera_event
from app.services.entry_v2_forwarder import (
    close_entry_v2_http_client,
    start_entry_v2_http_client,
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
from starlette.concurrency import run_in_threadpool

logger = get_logger(__name__)
_STUCK_HEAD_LOG_EVERY = 15
_api_worker: Optional["CameraIngestWorker"] = None


def wake_camera_ingest_worker() -> None:
    """Wake the API-owned child process after a newly durable receipt."""
    worker = _api_worker
    if worker is not None:
        worker.wake()


async def drain_camera_ingest_records_once() -> bool:
    """Drain the queue once and return whether its retryable head remains."""
    with spool.exclusive_consumer_lock() as owns_consumer:
        if not owns_consumer:
            # Another API worker owns the ordered consumer. Try again after the
            # normal retry cadence in case it exits or releases the lease.
            return True
        return await _drain_owned_camera_ingest_records_once()


async def _camera_configuration_available(
    raw_body: bytes, camera_ip: str, content_type: str
) -> bool:
    """Refresh an absent camera mapping before deciding a receipt is consumable."""
    try:
        event = await run_in_threadpool(
            parse_camera_event, raw_body, camera_ip, content_type
        )
    except CameraPayloadRejected:
        # The normal processor records the terminal malformed-payload outcome.
        return True
    if event.camera_id in settings.CAMERAS:
        return True

    # A child spawned while SQL was down has only its .env mapping. Refresh on
    # every unresolved retry so it never terminally discards an acknowledged
    # event just because the gateway inventory became reachable later.
    from app.services.camera_loader import load_cameras_from_db

    await run_in_threadpool(load_cameras_from_db)
    event = await run_in_threadpool(parse_camera_event, raw_body, camera_ip, content_type)
    return event.camera_id in settings.CAMERAS


async def _drain_owned_camera_ingest_records_once() -> bool:
    for path in spool.iter_spooled_records():
        try:
            header, raw_body = spool.read_record(path)
        except spool.SpoolRecordMalformed as exc:
            spool.quarantine_record(path, str(exc))
            continue
        except OSError:
            return False

        if int(header.get("attempts") or 0) >= settings.CAMERA_INGEST_MAX_ATTEMPTS:
            logger.warning(
                "[IngestSpool] dropping %s: persisted attempts=%s reached limit=%s",
                os.path.basename(path), header.get("attempts"), settings.CAMERA_INGEST_MAX_ATTEMPTS,
            )
            spool.remove_record(path)
            continue

        camera_ip = str(header.get("camera_ip") or "")
        content_type = str(header.get("content_type") or "")
        if not await _camera_configuration_available(raw_body, camera_ip, content_type):
            outcome = CameraEventOutcome(
                status="retry", detail="camera configuration unavailable"
            )
        else:
            db = SessionLocal()
            try:
                outcome = await process_camera_event(raw_body, camera_ip, content_type, db)
            finally:
                db.close()

        if outcome.retryable:
            attempts = spool.record_attempt(path, header)
            if attempts >= settings.CAMERA_INGEST_MAX_ATTEMPTS:
                logger.warning(
                    "[IngestSpool] dropping %s after %s failed attempts (limit=%s): %s",
                    os.path.basename(path), attempts, settings.CAMERA_INGEST_MAX_ATTEMPTS,
                    outcome.detail,
                )
                spool.remove_record(path)
                continue
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
            return True

        if outcome.status != "ok":
            spool.quarantine_record(
                path,
                f"terminal {outcome.status}: {outcome.detail or 'no detail'}",
            )
            logger.warning(
                "[IngestSpool] retained terminal %s outcome for %s",
                outcome.status,
                os.path.basename(path),
            )
            continue

        logger.info(
            "[IngestSpool] replayed %s -> %s (spooled %s)",
            os.path.basename(path),
            outcome.status,
            header.get("received_at"),
        )
        spool.remove_record(path)
    return False


async def _run(stop_event, wake_event, notification_queue=None) -> None:
    # The API process refreshes the .env inventory from the gateway DB at
    # startup. Spawn starts from imports, not that mutated in-memory mapping,
    # so the replay process must do the same before parsing any record.
    from app.services.camera_loader import load_cameras_from_db

    load_cameras_from_db()
    if notification_queue is not None:
        from app.services.camera_ingest_notifications import (
            install_child_notification_forwarder,
        )

        install_child_notification_forwarder(notification_queue)
    await start_entry_v2_http_client()
    await start_core_backend_http_client()
    await start_hikcentral_http_client()
    try:
        retry_delay = float(settings.CAMERA_INGEST_DRAIN_INTERVAL_SECONDS)
        blocked = False
        while not stop_event.is_set():
            if blocked:
                # New receipts cannot pass the failed head without reordering.
                # Keep the configured retry backoff even when they wake the API.
                await asyncio.to_thread(stop_event.wait, retry_delay)
            else:
                # A new receipt wakes normal processing immediately.
                await asyncio.to_thread(wake_event.wait)
                wake_event.clear()
            if stop_event.is_set():
                break
            try:
                blocked = await drain_camera_ingest_records_once()
            except Exception:
                # The record remains on disk. Do not let a transient worker bug
                # kill all future retries or change API receipt behavior.
                logger.exception("[IngestSpool] replay worker tick failed")
                blocked = True
    finally:
        await close_entry_v2_http_client()
        await close_core_backend_http_client()
        await close_hikcentral_http_client()


def run_camera_ingest_worker(stop_event, wake_event, notification_queue=None) -> None:
    """Multiprocessing target. Spawn keeps async globals separate from API."""
    asyncio.run(_run(stop_event, wake_event, notification_queue))


class CameraIngestWorker:
    """Lifecycle and wake handle held only by the API process."""

    def __init__(
        self,
        *,
        worker_target: Callable = run_camera_ingest_worker,
        worker_args: tuple = (),
    ) -> None:
        """Create the isolated process; injectable target supports isolation tests."""
        context = multiprocessing.get_context("spawn")
        self._worker_target = worker_target
        self._worker_args = worker_args
        self._stop_event = context.Event()
        self._wake_event = context.Event()
        self._process = self._new_process(context)

    def _new_process(self, context):
        return context.Process(
            target=self._worker_target,
            args=(self._stop_event, self._wake_event, *self._worker_args),
            name="camera-ingest-replay",
            daemon=True,
        )

    def start(self) -> None:
        global _api_worker
        _api_worker = self
        self.ensure_running()
        self.wake()

    def ensure_running(self) -> None:
        """Restart an exited child so accepted records never wait indefinitely."""
        if self._process.is_alive():
            return
        if self._process.pid is not None:
            logger.error("[IngestSpool] replay worker exited; restarting it")
            context = multiprocessing.get_context("spawn")
            self._process = self._new_process(context)
        self._process.start()

    def wake(self) -> None:
        self.ensure_running()
        self._wake_event.set()

    async def stop(self) -> None:
        global _api_worker
        self._stop_event.set()
        self._wake_event.set()
        await asyncio.to_thread(self._process.join, 2.0)
        if self._process.is_alive():
            logger.warning("[IngestSpool] replay worker did not exit; terminating it")
            self._process.terminate()
            await asyncio.to_thread(self._process.join, 1.0)
        if _api_worker is self:
            _api_worker = None
