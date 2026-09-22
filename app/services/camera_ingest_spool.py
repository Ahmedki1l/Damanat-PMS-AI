# app/services/camera_ingest_spool.py
"""Durable spool for camera events PMS-AI could not finish processing on arrival.

WHY THIS EXISTS
---------------
Authoritative Entry V2 used to answer the camera with HTTP 503 whenever VA, the
database or the entry lock was unavailable, on the stated assumption that the
camera is "the camera-facing retry boundary" (``routers/events.py``). Hikvision
HTTP listening-host push is fire-and-forget: it ignores ``Retry-After`` and never
re-POSTs. Measured over one production week, 332 camera picture IDs arrived and
all 332 were distinct — not a single event was ever re-delivered. So the retry
boundary had no retrier behind it and every 503 deleted a car: 218 of 1,951
events, 11.2%.

This module keeps the consistency guarantee that motivated the 503 (nothing is
committed that VA has not confirmed) and moves the retry into PMS-AI, where a
retrier actually exists. The event is written to disk, the camera is told 200,
and a drainer replays it until it succeeds.

RECORD FORMAT
-------------
One file per event: a single-line JSON header, a newline, then the raw request
body verbatim::

    {"received_at": "...", "camera_ip": "...", "content_type": "...", ...}\n
    <raw bytes exactly as the camera sent them>

The body is stored raw rather than base64 so that image-bearing multipart events
(mean 377 KB, max 1.2 MB in production) cost their true size on disk. Writes use
the same durability sequence proven in ``utils/core_backend_client.py``:
temp file -> fsync -> atomic rename -> directory fsync, so a crash mid-write can
never leave a half-record that the drainer would read.
"""

import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

RECORD_SUFFIX = ".evt"
TEMP_SUFFIX = ".tmp"
QUARANTINE_DIRNAME = "quarantine"
BOOT_MARKER_NAME = "_boot_marker.json"

# Set once at startup by check_spool_durability(); surfaced on /api/health so the
# answer does not depend on catching one line in the boot logs.
_durability: dict = {"checked": False, "durable": None, "detail": "not checked"}

# One warning per distinct degradation reason, not one per rejected event — a
# full volume during the morning peak must not turn into a log flood.
_reported_degradations: set[str] = set()
_spool_write_lock = threading.Lock()


class SpoolRecordMalformed(ValueError):
    """A spool file is readable but cannot be interpreted as a record."""


class ReplayOwnerUnavailable(RuntimeError):
    """Another API process already owns authoritative replay for this spool."""


def spool_dir() -> str:
    return settings.CAMERA_INGEST_SPOOL_DIR


def _quarantine_dir() -> str:
    return os.path.join(spool_dir(), QUARANTINE_DIRNAME)


class ReplayOwnerLease:
    """One API-process lifetime lease for authoritative replay and its SSE bridge."""

    def __init__(self, fd: int) -> None:
        self._fd: Optional[int] = fd

    @classmethod
    def acquire(cls) -> "ReplayOwnerLease":
        try:
            import fcntl
        except ImportError as exc:
            raise ReplayOwnerUnavailable(
                "authoritative camera spool requires POSIX file locking"
            ) from exc
        os.makedirs(spool_dir(), exist_ok=True)
        fd = os.open(os.path.join(spool_dir(), ".replay-owner.lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            raise ReplayOwnerUnavailable(
                "another API process owns authoritative replay for " + spool_dir()
            ) from exc
        return cls(fd)

    def release(self) -> None:
        if self._fd is None:
            return
        import fcntl

        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


@contextmanager
def exclusive_consumer_lock():
    """Yield whether this process exclusively owns the spool consumer lease.

    Every API worker may receive camera traffic, but only one may drain a shared
    PersistentVolume at a time. ``flock`` is released by the kernel when a
    crashed consumer exits, so there is no stale lock file recovery path.
    """
    try:
        import fcntl
    except ImportError:
        logger.error("[IngestSpool] no file-lock implementation; replay paused")
        yield False
        return
    fd: Optional[int] = None
    try:
        os.makedirs(spool_dir(), exist_ok=True)
        fd = os.open(os.path.join(spool_dir(), ".consumer.lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    except OSError as exc:
        logger.error("[IngestSpool] could not acquire consumer lease: %s", exc)
        yield False
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def _warn_once(key: str, message: str, *args) -> None:
    if key in _reported_degradations:
        return
    _reported_degradations.add(key)
    logger.error(message, *args)


def _fsync_directory(directory: str, *, required: bool = False) -> None:
    """Persist a rename on platforms that expose directory file descriptors.

    Windows has no directory fd, so the rename's durability is left to the
    filesystem there. Callers that acknowledge a newly-created record set
    ``required`` so a POSIX directory-sync failure cannot be reported as a
    durable write. Cleanup paths deliberately keep their best-effort behaviour.
    """
    if os.name == "nt":
        return
    fd = None
    try:
        fd = os.open(directory, os.O_RDONLY)
        os.fsync(fd)
    except OSError:
        if required:
            raise
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


# ── capacity ────────────────────────────────────────────────────────────────

def _spool_bytes() -> int:
    """Bytes held by the spool, INCLUDING quarantine.

    Nothing prunes quarantine, and it shares a volume with the live snapshot
    store, so excluding it would let it grow past the cap unnoticed until the
    free-space floor trips and events start being refused again.
    """
    total = 0
    for directory in (spool_dir(), _quarantine_dir()):
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_file() and (
                        entry.name.endswith(RECORD_SUFFIX)
                        or entry.name.endswith(RECORD_SUFFIX + ".reason.json")
                    ):
                        try:
                            total += entry.stat().st_size
                        except OSError:
                            continue
        except (FileNotFoundError, OSError):
            continue
    return total


def _free_bytes(path: str) -> Optional[int]:
    """Free space on the volume that holds (or will hold) ``path``.

    The capacity check runs before the spool directory is created, and
    ``disk_usage`` raises on a path that does not exist yet. Walking up to the
    nearest existing ancestor gives the same answer — it is the same volume —
    and keeps the free-space floor armed on the very first spooled event instead
    of silently skipping it until the directory happens to exist.
    """
    candidate = os.path.abspath(path)
    while True:
        try:
            return shutil.disk_usage(candidate).free
        except OSError:
            parent = os.path.dirname(candidate)
            if not parent or parent == candidate:
                return None
            candidate = parent


def _capacity_refusal(incoming_bytes: int = 0) -> Optional[str]:
    """Return a reason to refuse spooling, or None when there is room.

    In production the ingest spool shares a volume with ``detection_images``,
    which is the live snapshot store. A backlog must never be allowed to starve
    snapshot writes, so the free-space floor is checked before the spool's own
    size cap.
    """
    directory = spool_dir()
    free = _free_bytes(directory)
    if (
        free is not None
        and free - incoming_bytes < settings.CAMERA_INGEST_SPOOL_MIN_FREE_BYTES
    ):
        return (
            f"only {(free - incoming_bytes) // (1024 * 1024)} MB free after receipt "
            "on the spool volume "
            f"(floor is {settings.CAMERA_INGEST_SPOOL_MIN_FREE_BYTES // (1024 * 1024)} MB)"
        )
    used = _spool_bytes()
    if used + incoming_bytes > settings.CAMERA_INGEST_SPOOL_MAX_BYTES:
        return (
            f"spool would hold {(used + incoming_bytes) // (1024 * 1024)} MB, above its "
            f"{settings.CAMERA_INGEST_SPOOL_MAX_BYTES // (1024 * 1024)} MB cap"
        )
    return None


@contextmanager
def _spool_write_reservation(directory: str) -> Iterator[None]:
    """Serialize quota check and receipt creation across local API processes."""
    try:
        import fcntl
    except ImportError as exc:
        raise OSError("camera spool requires POSIX file locking") from exc
    with _spool_write_lock:
        fd = os.open(os.path.join(directory, ".write-reservation.lock"), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


# ── writing ─────────────────────────────────────────────────────────────────

def spool_camera_event(
    raw_body: bytes,
    camera_ip: str,
    content_type: str,
    reason: str,
    *,
    evidence_id: Optional[str] = None,
) -> bool:
    """Persist one undelivered camera event. True when it is safely on disk.

    A False return is the caller's signal to fall back to the legacy 503, which
    is worse but is the honest answer when the event cannot be stored. This
    function never raises: failing to spool must degrade the response, never
    take the camera webhook down.
    """
    directory = spool_dir()
    temp_path: Optional[str] = None
    try:
        os.makedirs(directory, exist_ok=True)
        received_at = datetime.now(timezone.utc)
        header = {
            "received_at": received_at.isoformat(),
            "camera_ip": camera_ip,
            "content_type": content_type,
            "reason": reason,
            "evidence_id": evidence_id,
            "attempts": 0,
        }
        encoded_header = json.dumps(header).encode("utf-8")
        name = (
            f"cam_{received_at.strftime('%Y%m%d_%H%M%S_%f')}_"
            f"{uuid.uuid4().hex[:8]}{RECORD_SUFFIX}"
        )
        receipt_bytes = len(encoded_header) + 1 + len(raw_body)
        with _spool_write_reservation(directory):
            refusal = _capacity_refusal(receipt_bytes)
            if refusal:
                _warn_once(
                    f"capacity:{refusal}",
                    "[IngestSpool] refusing to spool — %s. Falling back to a "
                    "camera-facing 503; events will be LOST until space is freed.",
                    refusal,
                )
                return False
            temp_path = os.path.join(directory, name + TEMP_SUFFIX)
            with open(temp_path, "wb") as handle:
                handle.write(encoded_header)
                handle.write(b"\n")
                handle.write(raw_body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, os.path.join(directory, name))
            temp_path = None
            # A file fsync alone does not make the rename durable. Do not tell the
            # camera the event is safely queued when the directory entry could not
            # be synced; the existing record is left in place for later recovery.
            _fsync_directory(directory, required=True)
        logger.info(
            "[IngestSpool] spooled camera event from %s (%s) — reason=%s",
            camera_ip,
            content_type or "unknown content-type",
            reason,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - degrade, never raise
        logger.error("[IngestSpool] failed to spool camera event: %s", exc, exc_info=True)
        if temp_path:
            try:
                os.remove(temp_path)
            except OSError:
                pass
        return False


# ── reading ─────────────────────────────────────────────────────────────────

def _record_paths(*, quarantine_unreadable: bool = True) -> list[str]:
    """Spooled record paths, oldest first.

    Ordered by the ``received_at`` embedded in each record rather than by mtime
    or directory order. Entry bursts depend on ordering — CAM-23, CAM-03 and the
    ANPR read for one car arrive seconds apart and must be replayed in the order
    the cameras produced them, not the order the filesystem happens to list.
    """
    directory = spool_dir()
    entries: list[tuple[str, str]] = []
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.error("[IngestSpool] cannot list %s: %s", directory, exc)
        return []

    for name in names:
        if not name.endswith(RECORD_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            received_at = str(read_header(path).get("received_at") or "")
        except SpoolRecordMalformed:
            # Only the drainer may move files. `spool_stats()` is called from a
            # GET /health probe, and a health check must not mutate the spool.
            if quarantine_unreadable:
                quarantine_record(path, "unreadable header")
                continue
            received_at = ""
        except OSError:
            continue
        # Fall back to the filename, which embeds the same timestamp, so a record
        # with a damaged header field still sorts sanely instead of jumping first.
        entries.append((received_at or name, path))

    entries.sort(key=lambda item: item[0])
    return [path for _, path in entries]


def iter_spooled_records() -> Iterator[str]:
    yield from _record_paths()


# A header is one JSON line; anything beyond this is malformed, not a big header.
# Bounded so a corrupt record cannot make a header read pull an arbitrary amount
# of a multi-MB file into memory.
_MAX_HEADER_BYTES = 64 * 1024


def _parse_header(blob: bytes, path: str) -> dict:
    newline = blob.find(b"\n")
    if newline < 0:
        raise SpoolRecordMalformed(f"{path} has no header terminator")
    try:
        header = json.loads(blob[:newline].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SpoolRecordMalformed(f"{path} has an unreadable header") from exc
    if not isinstance(header, dict):
        raise SpoolRecordMalformed(f"{path} header is not an object")
    return header


def read_header(path: str) -> dict:
    """Parse one record's header WITHOUT reading its body off disk.

    Records carry raw image bytes (mean 377 KB, max 1.2 MB). Listing and sorting
    the spool only needs the header, and `spool_stats()` runs on every /health
    scrape — reading whole bodies there would make a k8s probe read the entire
    backlog from disk, time out, and restart the pod during exactly the outage
    the spool exists to survive.
    """
    with open(path, "rb") as handle:
        return _parse_header(handle.read(_MAX_HEADER_BYTES), path)


def read_record(path: str) -> tuple[dict, bytes]:
    """Return (header, raw_body) for one spooled record."""
    with open(path, "rb") as handle:
        blob = handle.read()
    header = _parse_header(blob, path)
    return header, blob[blob.find(b"\n") + 1:]


def record_attempt(path: str, header: dict) -> int:
    """Increment and persist the replay attempt count. Returns the new count."""
    attempts = int(header.get("attempts") or 0) + 1
    header["attempts"] = attempts
    try:
        _, body = read_record(path)
        temp_path = path + TEMP_SUFFIX
        with open(temp_path, "wb") as handle:
            handle.write(json.dumps(header).encode("utf-8"))
            handle.write(b"\n")
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except (OSError, SpoolRecordMalformed) as exc:
        # The count is an optimisation for the poison guard, not correctness.
        # Losing it costs extra retries, never a lost event.
        logger.warning("[IngestSpool] could not persist attempt count for %s: %s", path, exc)
    return attempts


def record_age_seconds(header: dict) -> Optional[float]:
    """Seconds since the camera sent this event, or None if unparseable."""
    try:
        received = datetime.fromisoformat(str(header["received_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc) - received).total_seconds()


def remove_record(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.warning("[IngestSpool] could not remove drained record %s: %s", path, exc)


def quarantine_record(path: str, why: str) -> None:
    """Move a record aside so it stops consuming drain cycles but is not lost."""
    try:
        target_dir = _quarantine_dir()
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, os.path.basename(path))
        reason_path = target + ".reason.json"
        temp_path = reason_path + TEMP_SUFFIX
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump({"reason": why}, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, reason_path)
        _fsync_directory(target_dir, required=True)
        os.replace(path, target)
        # The sidecar fsync above only persists the inspection reason. The
        # acknowledged raw receipt becomes durable in quarantine only after
        # this post-move target-directory sync; then sync its removal at source.
        _fsync_directory(target_dir, required=True)
        _fsync_directory(os.path.dirname(path), required=True)
        logger.warning("[IngestSpool] quarantined %s — %s", os.path.basename(path), why)
    except OSError as exc:
        logger.error("[IngestSpool] could not quarantine %s: %s", path, exc)


# ── durability self-check ───────────────────────────────────────────────────

def check_spool_durability() -> dict:
    """Detect at boot whether the spool directory survives a restart.

    Production has exactly one PersistentVolume (``detection_images``), so unless
    CAMERA_INGEST_SPOOL_DIR points inside it the spool lives in the container's
    writable layer and is wiped on every pod restart. That would move the event
    loss from "deleted at the 503" to "deleted at the next restart" — rarer, but
    the same lost car, and silent.

    Rather than trusting a deployment manifest nobody re-reads, each boot leaves
    a marker and looks for the previous one. This tests the actual behaviour, so
    it also catches an ``emptyDir`` (which looks like a mounted volume in YAML
    but is equally ephemeral) and any future manifest change that drops it.

    Reports; never raises. A raising validator for an optional dependency is what
    crash-looped this deployment once already.
    """
    global _durability
    directory = spool_dir()
    marker_path = os.path.join(directory, BOOT_MARKER_NAME)
    now = datetime.now(timezone.utc).isoformat()
    previous: Optional[str] = None

    try:
        os.makedirs(directory, exist_ok=True)
        if os.path.isfile(marker_path):
            try:
                with open(marker_path, "r", encoding="utf-8") as handle:
                    previous = str(json.load(handle).get("booted_at") or "") or None
            except (OSError, ValueError):
                previous = None

        with open(marker_path, "w", encoding="utf-8") as handle:
            json.dump({"booted_at": now, "previous_boot": previous}, handle)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _durability = {
            "checked": True,
            "durable": False,
            "detail": f"spool directory is not writable: {exc}",
            "path": directory,
        }
        logger.error(
            "[IngestSpool] spool directory %s is NOT WRITABLE (%s). Camera events "
            "cannot be spooled at all; the webhook will fall back to 503s.",
            directory,
            exc,
        )
        return _durability

    free = _free_bytes(directory)
    free_mb = f"{free // (1024 * 1024)} MB free" if free is not None else "free space unknown"

    if previous:
        _durability = {
            "checked": True,
            "durable": True,
            "detail": f"marker from previous boot {previous} survived restart",
            "path": directory,
            "previous_boot": previous,
        }
        logger.info(
            "[IngestSpool] storage is DURABLE — marker from previous boot %s "
            "survived a restart. path=%s (%s)",
            previous,
            directory,
            free_mb,
        )
    else:
        # A genuinely first-ever boot looks identical to ephemeral storage. Say so
        # honestly instead of guessing; the second boot is unambiguous.
        _durability = {
            "checked": True,
            "durable": None,
            "detail": "no previous boot marker — first boot, or storage is ephemeral",
            "path": directory,
        }
        logger.warning(
            "[IngestSpool] no marker from a previous boot at %s (%s). This is "
            "EITHER the first boot after enabling the spool, OR the directory is "
            "ephemeral and spooled events will NOT survive a pod restart. Restart "
            "the pod once and re-read this line to tell the two apart. Production "
            "has one PersistentVolume (detection_images) — if this path is not "
            "inside it, storage is ephemeral.",
            directory,
            free_mb,
        )
    return _durability


def spool_stats() -> dict:
    """Snapshot for /api/health. Cheap enough to call on every scrape."""
    if not settings.CAMERA_INGEST_SPOOL_ENABLED:
        return {"enabled": False, "path": spool_dir(), "depth": 0, "bytes": 0,
                "oldest_age_seconds": None, "durability": _durability}
    paths = _record_paths(quarantine_unreadable=False)
    oldest_age: Optional[float] = None
    if paths:
        try:
            header = read_header(paths[0])
            received = datetime.fromisoformat(str(header["received_at"]))
            oldest_age = (datetime.now(timezone.utc) - received).total_seconds()
        except (KeyError, ValueError, OSError, SpoolRecordMalformed):
            oldest_age = None
    return {
        "enabled": settings.CAMERA_INGEST_SPOOL_ENABLED,
        "path": spool_dir(),
        "depth": len(paths),
        "bytes": _spool_bytes(),
        "oldest_age_seconds": oldest_age,
        "durability": _durability,
    }
