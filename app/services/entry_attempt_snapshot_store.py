"""Durable entry-image association keyed by Entry V2 attempt identity."""

import hashlib
import os
from pathlib import Path
import tempfile

from app.services import snapshot_service
from app.utils.logger import get_logger

logger = get_logger(__name__)


class EntryAttemptSnapshotStorageError(RuntimeError):
    """The camera must retry because entry evidence could not be retained."""


def _snapshot_path(attempt_id: str) -> Path:
    digest = hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()
    return Path(snapshot_service.SNAPSHOT_DIR) / f"entry_attempt_{digest}.jpg"


def persist_entry_attempt_snapshot(attempt_id: str, image: bytes) -> str:
    """Persist one forwarded vehicle crop without replacing an earlier image."""
    if not attempt_id:
        raise EntryAttemptSnapshotStorageError("entry attempt identity is empty")
    if not image:
        raise EntryAttemptSnapshotStorageError("entry image is empty")

    snapshot_path = _snapshot_path(attempt_id)
    temporary_path: str | None = None
    try:
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot_path.is_file():
            return _public_snapshot_url(snapshot_path)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{snapshot_path.name}.",
            dir=snapshot_path.parent,
        )
        with os.fdopen(descriptor, "wb") as image_file:
            image_file.write(image)
            image_file.flush()
            os.fsync(image_file.fileno())
        try:
            os.link(temporary_path, snapshot_path)
        except FileExistsError:
            pass
        directory_descriptor = os.open(snapshot_path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as exc:
        raise EntryAttemptSnapshotStorageError(
            f"could not persist entry evidence for attempt {attempt_id}"
        ) from exc
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning(
                    "[EntryV2] Could not remove temporary entry snapshot %s: %r",
                    temporary_path,
                    exc,
                )

    return _public_snapshot_url(snapshot_path)


def resolve_entry_attempt_snapshot(attempt_id: str | None) -> str | None:
    """Return the existing public snapshot URL for this exact attempt only."""
    if not attempt_id:
        return None
    snapshot_path = _snapshot_path(attempt_id)
    if not snapshot_path.is_file():
        return None
    return _public_snapshot_url(snapshot_path)


def _public_snapshot_url(snapshot_path: Path) -> str:
    public_url = snapshot_service.to_public_snapshot_url(str(snapshot_path))
    if public_url is None:
        raise EntryAttemptSnapshotStorageError("snapshot URL could not be created")
    return public_url
