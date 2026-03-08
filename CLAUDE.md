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

# Docker (starts PostgreSQL + backend)
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
  → service handler (occupancy/violation/intrusion/entry_exit)
  → alert_service.py (shared alert creation)
  → PostgreSQL persistence
```

There is also a camera polling mode (`camera_poller.py`) that connects TO cameras via their ISAPI `alertStream` endpoint — currently commented out in `main.py` startup.

### Layer Responsibilities

- **Routers** (`app/routers/`): HTTP endpoints, request/response handling. Each file maps to a use case.
- **Services** (`app/services/`): All business logic. `event_parser.py` normalizes raw payloads into `ParsedCameraEvent`. `event_dispatcher.py` routes events to the correct handler. Each handler (occupancy, violation, intrusion, entry_exit) processes its use case and calls `alert_service.create_alert()`.
- **Models** (`app/models/`): SQLAlchemy ORM definitions. 5 tables: `camera_events`, `zone_occupancy`, `alerts`, `vehicles`, `entry_exit_log`.
- **Schemas** (`app/schemas/`): Pydantic request/response models.
- **Config** (`app/config.py`): Pydantic BaseSettings. Contains camera inventory (`CAMERAS` dict), IP-to-camera-ID mapping (`CAMERA_IP_MAP`), and thresholds.

### Security

Optional API key auth via `APIKeyMiddleware` in `main.py`. Camera webhook (`/events/camera`), health, alerts, and docs endpoints are always open (no auth). Set `API_KEY` in `.env` to enable; leave empty to disable.

### Phase 1 vs Phase 2

Phase 1 (active): Occupancy monitoring (UC3), violation detection (UC5), intrusion detection (UC6) — uses XML/ISAPI events from bullet/dome cameras.

Phase 2 (pre-built, routers commented out in `main.py`): Entry/exit logging (UC1), parking statistics (UC2), vehicle management (UC4) — uses JSON/ANPR events from LPR cameras. To activate: uncomment Phase 2 router includes in `app/main.py`.

Phase 2 services are imported conditionally in `event_dispatcher.py` with try/except ImportError, so Phase 1 never breaks.

## Key Conventions

- **Webhook endpoint must always return HTTP 200** — never let exceptions bubble up to cameras. The `/api/v1/events/camera` endpoint catches all errors and returns 200 regardless.
- **Logging**: Use `from app.utils.logger import get_logger; logger = get_logger(__name__)` — never use `print()`. Logs go to both console and rotating files in `logs/`. Use structured format: `logger.info(f"[UC3] zone={zone_id} count={count}")`.
- **Configuration**: Never hardcode IPs or credentials. Use `settings` from `app/config.py` which reads from `.env`.
- **Database sessions**: Use FastAPI dependency injection (`db: Session = Depends(get_db)`). Explicit `db.commit()` after modifications.
- **New camera**: Add to both `CAMERAS` and `CAMERA_IP_MAP` in `config.py`.
- **New event handler**: Create service in `services/`, register routing in `event_dispatcher.py`.
- **New API endpoint**: Create router in `routers/`, include in `main.py`.
- **New DB table**: Add model in `models/`, import in `models/__init__.py`.

## Testing

Tests use pytest + pytest-asyncio. Database sessions are mocked with `MagicMock`/`AsyncMock` — no real database needed for unit tests. Service dependencies like `create_alert` are patched.

## Stack

FastAPI 0.110, SQLAlchemy 2.0, PostgreSQL 16, Pydantic 2.6, lxml (XML parsing), Python 3.11+

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


# Project: Damanat-PMS-AI

## Dev Conventions
- Clean architecture / layered structure
- Preferred patterns: [your patterns]
- Testing approach: [your approach]
- Do not touch: [sensitive areas]

## Before every task
- Review affected modules before suggesting changes
- Prefer refactor over rewrite unless justified