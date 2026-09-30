"""ASGI contract for liveness and dependency diagnostics."""

import asyncio
from threading import Event

import anyio.to_thread
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.database import get_db
from app.main import APIKeyMiddleware, app
from app.routers import health

_NO_OVERRIDE = object()


def _restore_db_override(previous_override):
    if previous_override is _NO_OVERRIDE:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous_override


def test_health_returns_200_without_database_or_spool_access(monkeypatch):
    def unavailable_db():
        raise AssertionError("liveness opened a database session")

    def unavailable_spool():
        raise AssertionError("liveness scanned the spool")

    previous_override = app.dependency_overrides.get(get_db, _NO_OVERRIDE)
    app.dependency_overrides[get_db] = unavailable_db
    monkeypatch.setattr("app.services.camera_ingest_spool.spool_stats", unavailable_spool)
    try:
        response = TestClient(app).get("/api/v1/health")
    finally:
        _restore_db_override(previous_override)

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database"] == "not_checked"
    assert response.json()["camera_ingest_spool"] == {"status": "not_checked"}


def test_diagnostics_reports_database_failure_and_spool_state(monkeypatch):
    class UnavailableDB:
        def execute(self, statement):
            raise RuntimeError("database unavailable")

    def unavailable_db():
        yield UnavailableDB()

    previous_override = app.dependency_overrides.get(get_db, _NO_OVERRIDE)
    app.dependency_overrides[get_db] = unavailable_db
    monkeypatch.setattr(
        "app.services.camera_ingest_spool.spool_stats",
        lambda: {"depth": 3, "durability": "ok"},
    )
    try:
        response = TestClient(app).get("/api/v1/health/diagnostics")
    finally:
        _restore_db_override(previous_override)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error: database unavailable"
    assert body["camera_ingest_spool"]["depth"] == 3


def test_diagnostics_requires_api_key_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "API_KEY", "test-secret")
    protected_app = FastAPI()
    protected_app.add_middleware(APIKeyMiddleware)
    protected_app.include_router(health.router, prefix="/api/v1")

    class AvailableDB:
        def execute(self, statement):
            return None

    protected_app.dependency_overrides[get_db] = lambda: AvailableDB()
    monkeypatch.setattr(
        "app.services.camera_ingest_spool.spool_stats", lambda: {"depth": 0}
    )
    client = TestClient(protected_app)

    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/health/diagnostics").status_code == 401
    authorized = client.get(
        "/api/v1/health/diagnostics", headers={"X-API-Key": "test-secret"}
    )
    assert authorized.status_code == 200
    assert authorized.json()["database"] == "ok"


@pytest.mark.asyncio
async def test_health_responds_while_diagnostics_waits_on_database(monkeypatch):
    db_query_started = Event()
    release_db_query = Event()

    class SlowDB:
        def execute(self, statement):
            db_query_started.set()
            assert release_db_query.wait(timeout=5)

    def slow_db():
        yield SlowDB()

    previous_override = app.dependency_overrides.get(get_db, _NO_OVERRIDE)
    app.dependency_overrides[get_db] = slow_db
    monkeypatch.setattr(
        "app.services.camera_ingest_spool.spool_stats", lambda: {"depth": 0}
    )
    limiter = anyio.to_thread.current_default_thread_limiter()
    previous_tokens = limiter.total_tokens
    limiter.total_tokens = 1
    try:
        # One ASGI event loop, with the only AnyIO worker held by diagnostics.
        # Skip lifespan to avoid starting unrelated external workers.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            diagnostics = asyncio.create_task(client.get("/api/v1/health/diagnostics"))
            try:
                assert await asyncio.to_thread(db_query_started.wait, 5)
                response = await asyncio.wait_for(client.get("/api/v1/health"), 2)
                assert response.status_code == 200
                assert response.json()["status"] == "ok"
                assert not diagnostics.done()
            finally:
                release_db_query.set()
                assert (await asyncio.wait_for(diagnostics, 5)).status_code == 200
    finally:
        release_db_query.set()
        limiter.total_tokens = previous_tokens
        _restore_db_override(previous_override)
