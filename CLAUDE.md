# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Damanat Parking Analytics AI Backend — a FastAPI service that processes real-time events from Hikvision cameras (ISAPI XML for Phase 1, JSON/ANPR for Phase 2) through an event-driven pipeline and exposes REST APIs for a parking management dashboard. Runs entirely on a LAN (no internet required). All AI processing happens on the camera edge — the backend only reacts to events.

## Commands

```bash
# Run dev server
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_event_parser.py -v

# Run a single test function
python -m pytest tests/test_event_parser.py::test_parse_xml_field_detection -v

# Initialize database tables
python scripts/setup/init_db.py

# Run Alembic migrations
alembic upgrade head
alembic revision --autogenerate -m "description"

# Docker (starts MySQL + backend)
docker-compose up -d
docker-compose up -d db

# Simulate camera events for testing
python scripts/test/simulate_event.py --event regionEntrance --zone parking-row-A --ip 192.168.1.103
python scripts/test/simulate_event.py --event anpr --plate ABC-1234 --ip 192.168.1.104
```

## Architecture

### Event-Driven Pipeline

All camera events flow through a single path:

```
Camera HTTP Push → POST /api/v1/events/camera
  → event_parser.py (auto-detect XML vs JSON, normalize to ParsedCameraEvent dataclass)
  → event_dispatcher.py (route by event_type to correct service handler)
  → service handler (occupancy / entry_exit)
  → alert_service.py (shared alert creation)
  → MySQL persistence + optional Node.js backend notification
```

There is also a camera polling mode (`camera_poller.py`) that connects TO cameras via their ISAPI `alertStream` endpoint — currently not wired up in `main.py` startup.

### Layer Responsibilities

- **Routers** (`app/routers/`): HTTP endpoints, request/response handling. Each file maps to a use case.
- **Services** (`app/services/`): All business logic. `event_parser.py` normalizes raw payloads into `ParsedCameraEvent`. `event_dispatcher.py` routes events to the correct handler. `occupancy_service.py` and `entry_exit_service.py` are the primary handlers. `snapshot_service.py` fetches/uploads camera snapshots.
- **Repositories** (`app/repositories/`): Data-access helpers that wrap ORM queries (e.g. `vehicle_repository.py`). Use these rather than writing raw ORM queries in services.
- **Models** (`app/models/`): SQLAlchemy ORM definitions. 6 tables: `camera_events`, `zone_occupancy`, `alerts`, `vehicles`, `entry_exit_log`, `system_config`.
- **Schemas** (`app/schemas/`): Pydantic request/response models.
- **Config** (`app/config.py`): Pydantic BaseSettings. Contains camera inventory (`CAMERAS` dict), IP-to-camera-ID mapping (`CAMERA_IP_MAP`), zone UUIDs, and thresholds.

### External Integrations

- **Node.js core backend** (`app/utils/core_backend_client.py`): Fire-and-forget HTTP push to the Node.js backend for occupancy, ANPR, and alert events. Configured via `NODEBACK_URL`, `NODEBACK_SITE_ID`, `NODEBACK_SERVICE_KEY`. If `NODEBACK_URL` is empty, all calls silently skip.
- **PMS Tracking API** (`PMS_API_URL`): Secondary HTTP push to the PMS tracking service. Empty = disabled.
- **DigitalOcean Spaces** (`app/utils/spaces_client.py`): Snapshot image upload. Enabled when `STORAGE_MODE=spaces`. Falls back gracefully when unreachable.

### Security

Optional API key auth via `APIKeyMiddleware` in `main.py`. Camera webhook (`/events/camera`), health, alerts, and docs endpoints are always open (no auth). Set `API_KEY` in `.env` to enable; leave empty to disable.

### Phase 1 vs Phase 2

Both phases are now active.

Phase 1: Occupancy monitoring (UC3), violation detection (UC5), intrusion detection (UC6) — XML/ISAPI events from bullet/dome cameras (CAM-01 through CAM-14, CAM-35).

Phase 2: Entry/exit logging (UC1), parking statistics (UC2), vehicle management (UC4) — JSON/ANPR events from LPR cameras (CAM-ENTRY, CAM-EXIT).

Occupancy cameras (hardcoded in `event_dispatcher.py`): CAM-03, CAM-08, CAM-09, CAM-10. To change which cameras drive occupancy, update that set and the corresponding `CAMERAS` gate assignments in `config.py`.

## Key Conventions

- **Webhook endpoint must always return HTTP 200** — never let exceptions bubble up to cameras. The `/api/v1/events/camera` endpoint catches all errors and returns 200 regardless.
- **Logging**: Use `from app.utils.logger import get_logger; logger = get_logger(__name__)` — never use `print()`. Logs go to both console and rotating files in `logs/`. Use structured format: `logger.info(f"[UC3] zone={zone_id} count={count}")`.
- **Configuration**: Never hardcode IPs or credentials. Use `settings` from `app/config.py` which reads from `.env`.
- **Database sessions**: Use FastAPI dependency injection (`db: Session = Depends(get_db)`). Explicit `db.commit()` after modifications.
- **Occupancy cache**: `occupancy_service.py` uses an in-memory dedup cache. Cache keys are returned from `handle_occupancy_event` and must be recorded by the router AFTER a successful `db.commit()` — not before — to avoid cache pollution on rollback.
- **New camera**: Add to `.env` (`CAM_XX_IP`, `CAM_XX_USER`, etc.) — `config.py` builds `CAMERAS` and `CAMERA_IP_MAP` automatically via `@model_validator`.
- **New event handler**: Create service in `services/`, register routing in `event_dispatcher.py`.
- **New API endpoint**: Create router in `routers/`, include in `main.py`.
- **New DB table**: Add model in `models/`, import in `models/__init__.py`, generate an Alembic migration.

## Testing

Tests use pytest + pytest-asyncio. Database sessions are mocked with `MagicMock`/`AsyncMock` — no real database needed for unit tests. Service dependencies like `create_alert` are patched.

## Stack

FastAPI 0.133, SQLAlchemy 2.0, MySQL (pymysql driver), Alembic 1.18, Pydantic 2.12, lxml (XML parsing), httpx (async HTTP), Python 3.11+

---

## Workflow Orchestration

### Plan Mode Default
- Enter plan mode for ANY non-trivial task (3+ steps or architectural decisions)
- If something goes sideways, STOP and re-plan immediately — don't keep pushing
- Use plan mode for verification steps, not just building
- Write detailed specs upfront to reduce ambiguity

### Subagent Strategy
- Use subagents liberally to keep main context window clean
- Offload research, exploration, and parallel analysis to subagents
- For complex problems, throw more compute at it via subagents
- One task per subagent for focused execution

### Self-Improvement Loop
- After ANY correction from the user: update tasks/lessons.md with the pattern
- Write rules for yourself that prevent the same mistake
- Ruthlessly iterate on these lessons until mistake rate drops
- Review lessons at session start for relevant project

### Verification Before Done
- Never mark a task complete without proving it works
- Diff behavior between main and your changes when relevant
- Ask yourself: "Would a staff engineer approve this?"
- Run tests, check logs, demonstrate correctness

### Demand Elegance (Balanced)
- For non-trivial changes: pause and ask "is there a more elegant way?"
- If a fix feels hacky: "Knowing everything I know now, implement the elegant solution"
- Skip this for simple, obvious fixes — don't over-engineer
- Challenge your own work before presenting it

### Autonomous Bug Fixing
- When given a bug report: just fix it. Don't ask for hand-holding
- Point at logs, errors, failing tests — then resolve them
- Zero context switching required from the user
- Go fix failing CI tests without being told how

## Task Management
1. Plan First: Write plan to tasks/todo.md with checkable items
2. Verify Plans: Check in before starting implementation
3. Track Progress: Mark items complete as you go
4. Explain Changes: High-level summary at each step
5. Document Results: Add review section to tasks/todo.md
6. Capture Lessons: Update tasks/lessons.md after corrections

## Core Principles
- **Simplicity First**: Make every change as simple as possible. Impact minimal code.
- **No Laziness**: Find root causes. No temporary fixes. Senior developer standards.
- **Minimal Impact**: Changes should only touch what's necessary. Avoid introducing bugs.

## Before Every Task
- Review affected modules before suggesting changes
- Prefer refactor over rewrite unless justified
