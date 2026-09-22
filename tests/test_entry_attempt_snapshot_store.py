import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import snapshots
from app.services import entry_attempt_snapshot_store as store


def test_attempt_snapshot_is_durable_and_resolves_without_process_state(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("app.services.snapshot_service.SNAPSHOT_DIR", str(tmp_path))

    url = store.persist_entry_attempt_snapshot("attempt-restart", b"image-bytes")

    assert store.resolve_entry_attempt_snapshot("attempt-restart") == url
    assert (tmp_path / url.rsplit("/", 1)[-1]).read_bytes() == b"image-bytes"


def test_attempt_snapshot_is_distinct_from_plate_and_never_overwritten(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("app.services.snapshot_service.SNAPSHOT_DIR", str(tmp_path))

    first_url = store.persist_entry_attempt_snapshot("same-plate-visit-1", b"first")
    second_url = store.persist_entry_attempt_snapshot("same-plate-visit-2", b"second")
    replay_url = store.persist_entry_attempt_snapshot("same-plate-visit-1", b"wrong")

    assert first_url != second_url
    assert replay_url == first_url
    assert (tmp_path / first_url.rsplit("/", 1)[-1]).read_bytes() == b"first"


def test_storage_failure_raises_a_retryable_persistence_error(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.snapshot_service.SNAPSHOT_DIR", str(tmp_path))

    def disk_full(*_args):
        raise OSError("full")

    monkeypatch.setattr(store.os, "link", disk_full)

    with pytest.raises(
        store.EntryAttemptSnapshotStorageError,
        match="could not persist",
    ):
        store.persist_entry_attempt_snapshot("attempt-storage-failure", b"image")


def test_persisted_attempt_image_is_served_by_the_snapshot_router(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("app.services.snapshot_service.SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setattr(snapshots, "SNAPSHOT_DIR", str(tmp_path))
    url = store.persist_entry_attempt_snapshot("attempt-fetch", b"entry-image")
    app = FastAPI()
    app.include_router(snapshots.router)

    response = TestClient(app).get(url)

    assert response.status_code == 200
    assert response.content == b"entry-image"
    assert response.headers["content-type"] == "image/jpeg"
