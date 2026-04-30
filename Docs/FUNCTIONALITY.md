# Damanat PMS AI — Server Functionality Reference

> FastAPI backend processing Hikvision camera events for parking management.  
> **Base URL**: `http://{BACKEND_IP}:{BACKEND_PORT}/api/v1`  
> **Docs**: `/docs` (Swagger) · `/redoc` (ReDoc)

---

## Architecture Overview

```
Hikvision Cameras (ISAPI)
    │
    ├── Push Mode: Camera POSTs to /api/v1/events/camera (webhook)
    └── Pull Mode: camera_poller.py connects to camera alertStream
            │
            ▼
    ┌──────────────────┐
    │   event_parser   │  Extracts XML/JSON/Multipart → ParsedCameraEvent
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ event_dispatcher │  Routes to UC1–UC6 handlers
    └────────┬─────────┘
             ├── occupancy_service    (UC3)
             ├── violation_service    (UC5)
             ├── intrusion_service    (UC6)
             └── entry_exit_service   (UC1/UC2/UC4)
                     │
                     ├── Local PostgreSQL DB
                     ├── Node.js Backend (parking-times)
                     └── PMS Tracking API (plate + image)
```

---

## Phase 1 — Smart Camera Analytics

### UC3: Parking Occupancy

Real-time vehicle counting across multi-level parking zones using line-crossing detection.

**How it works:**
- Cameras CAM-03, CAM-08, CAM-09, CAM-10 are designated occupancy cameras
- `linedetection` events with direction signals (`region_id` or `crossing_direction`) determine +1/-1 delta
- Atomic SQL updates (`func.max(..., 0)`) prevent negative counts
- Savepoint-wrapped multi-zone updates prevent cross-zone drift
- In-memory dedup cache (30s TTL) prevents double-counting

**Zones:**
| Zone | Cameras | Role |
|------|---------|------|
| GARAGE-TOTAL | CAM-03, CAM-08 | Grand total (main entry/exit) |
| B1-PARKING | CAM-03, CAM-08, CAM-09, CAM-10 | Basement 1 |
| B2-PARKING | CAM-09, CAM-10 | Basement 2 |

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/occupancy` | All zones — count, capacity, % |
| `GET` | `/occupancy/{zone_id}` | Single zone details |
| `PUT` | `/occupancy/{zone_id}/capacity` | Set max capacity |
| `PUT` | `/occupancy/{zone_id}/reset` | Reset count to zero |

---

### UC5: Violation Detection

Alerts when vehicles enter restricted zones (fielddetection / regionEntrance events).

**Restricted zones** (configurable via `RESTRICTED_ZONES` env var):
`restricted-vip`, `no-parking-zone`, `emergency-exit`, `loading-bay`

**Features:**
- Cooldown-based deduplication (default 30s)
- Auto-resolve on `regionExiting` events
- Snapshot attached to each violation

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/violations` | List violations (filter by camera, zone, resolved) |
| `PUT` | `/violations/{id}/resolve` | Resolve single violation |
| `PUT` | `/violations/resolve-all` | Bulk resolve all active violations |

---

### UC6: Intrusion Detection

Alerts for unauthorized access in monitored zones.

**Monitored zones** (configurable via `MONITORED_INTRUSION_ZONES` env var):
`emergency-exit`, `staff-only-area`, `after-hours-zone`

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/intrusions` | List intrusion alerts (filter by camera, zone, resolved) |
| `PUT` | `/intrusions/{id}/resolve` | Resolve single intrusion |
| `PUT` | `/intrusions/resolve-all` | Bulk resolve all active intrusions |

---

## Phase 2 — ANPR (License Plate Recognition)

### UC1: Entry/Exit Counting

Logs vehicle entry and exit events from ANPR cameras at the gates.

**Cameras:** `CAM-ENTRY` (entry gate), `CAM-EXIT` (exit gate)

**Features:**
- Saudi plate normalization (`9444HUD` → `HUD-9444`)
- 30s dedup window (camera sends duplicate XML + JSON per detection)
- 2-minute anti-bounce suppression (prevents false re-entry after exit)
- Multipart image extraction and Spaces CDN upload

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/entry-exit` | Entry/exit log (filter by gate) |
| `GET` | `/entry-exit/count/today` | Today's entries, exits, currently parked |

---

### UC2: Parking Duration

Calculates how long each vehicle was parked by matching entry/exit pairs.

**How it works:**
- On exit, finds the most recent unmatched entry for the same plate
- Calculates `duration_seconds = exit_time - entry_time`
- Links entry ↔ exit records via `matched_entry_id`

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/stats/parking-time` | Average parking duration (minutes) for a date |
| `GET` | `/stats/daily` | Daily summary: total vehicles + avg parking time |

---

### UC4: Vehicle Identity & Classification

Registry of known vehicles (employees, visitors) with plate lookup.

**Features:**
- CRUD for vehicle registration
- Automatic `unknown_vehicle` alert on unregistered plate detection
- Used by `entry_exit_service` to classify vehicles on detection

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/vehicles` | List registered vehicles (filter by type) |
| `POST` | `/vehicles` | Register a vehicle (plate, owner, type) |
| `DELETE` | `/vehicles/{plate}` | Remove a vehicle |
| `GET` | `/vehicles/lookup/{plate}` | Plate lookup (known/unknown, owner, type) |

---

## Event Ingestion

### Camera Webhook

Single entry point for all camera events (Phase 1 XML + Phase 2 JSON + multipart).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/events/camera` | Camera webhook — receives all events |
| `GET` | `/events` | Browse raw event log (filter by camera, type) |

**Flow:** `receive → parse → snapshot upload → dispatch to UC handlers → persist raw event → commit`

### Camera Polling (Pull Mode)

`camera_poller.py` connects to each camera's ISAPI `alertStream` endpoint and reads events in real-time. Used when cameras can't push webhooks. Features exponential backoff reconnection.

---

## External Integrations

### Node.js Backend (Vercel)

Forwards entry/exit events to `NODEBACK_URL` for MongoDB parking-times storage.

| Event | Endpoint |
|-------|----------|
| Entry | `POST /api/v1/sites/{siteId}/parking-times/entry` |
| Exit | `POST /api/v1/sites/{siteId}/parking-times/exit` |
| Occupancy +1 | `POST /api/v1/sites/{siteId}/occupancy/entry` |
| Occupancy -1 | `POST /api/v1/sites/{siteId}/occupancy/exit` |

Auth: `X-Service-Key` header.

### PMS Tracking API

Forwards plate + base64 image to `PMS_API_URL` on entry detection for vehicle tracking.

| Event | Endpoint | Body |
|-------|----------|------|
| Entry | `POST /api/anpr/event` | `{plate, direction, image_base64}` |

---

## Infrastructure

### Snapshots & Image Storage

| Mode | Config | Behavior |
|------|--------|----------|
| `local` | `STORAGE_MODE=local` | Saves to `detection_images/` |
| `spaces` | `STORAGE_MODE=spaces` | Uploads to DigitalOcean Spaces CDN |

Priority: multipart detection frame → Spaces upload → fallback camera snapshot fetch.

### Health Check

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Backend, DB, and camera reachability status |

### Alerts (Unified)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/alerts` | All alerts (filter by type: `violation`, `intrusion`, `unknown_vehicle`, `capacity_exceeded`) |

### Camera Log Filter

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/camera-filter` | Current include/exclude camera filters |
| `PUT` | `/set-camera-filter` | Update camera log filter |
| `DELETE` | `/camera-filter/{id}` | Remove a camera filter |

### Security

- Optional API key middleware (`API_KEY` env var)
- Camera webhook (`/events/camera`), health, alerts, and intrusion/violation endpoints are exempt from auth
- Node.js integration uses `X-Service-Key` service-to-service auth

---

## Database Models

| Model | Table | Purpose |
|-------|-------|---------|
| `CameraEvent` | `camera_events` | Raw event log from all cameras |
| `ZoneOccupancy` | `zone_occupancy` | Per-zone vehicle count + capacity |
| `Alert` | `alerts` | Violations, intrusions, unknown vehicle, capacity alerts |
| `EntryExitLog` | `entry_exit_logs` | ANPR gate events with parking duration |
| `Vehicle` | `vehicles` | Registered vehicle registry |

**DB**: PostgreSQL (Neon) · **ORM**: SQLAlchemy · **Migrations**: Alembic

---

## Configuration (`.env`)

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string |
| `BACKEND_IP` / `BACKEND_PORT` | Server bind address |
| `API_KEY` | Optional API auth (empty = disabled) |
| `NODEBACK_URL` / `NODEBACK_SITE_ID` / `NODEBACK_SERVICE_KEY` | Node.js backend integration |
| `PMS_API_URL` | PMS tracking API (plate forwarding) |
| `STORAGE_MODE` | `local` or `spaces` |
| `DO_SPACES_*` | DigitalOcean Spaces credentials |
| `CAM_XX_*` | Per-camera IP, user, password, name |
| `RESTRICTED_ZONES` / `MONITORED_INTRUSION_ZONES` | Zone lists |
| `LOG_LEVEL` | Logging level |
| `LOG_CAMERA_FILTER` / `LOG_CAMERA_EXCLUDE` | Selective event logging |
