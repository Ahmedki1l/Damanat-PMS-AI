# Damanat Parking Analytics Backend

Fully offline AI camera event processing system for Damanat parking facility (Saudi Arabia).

> **Phase 1**: Intrusion Detection, Parking Occupancy, Violation Alerts  
> **Phase 2**: ANPR Entry/Exit Counting, Parking Duration, Vehicle ID

---

## 🏗️ Architecture

```
Hikvision Cameras → PMS-AI webhook → Video Analytics (Entry V2) → PMS-AI DB/API → Dashboard
```

- **Fully Offline** — LAN only, no cloud or internet required
- **Event-Driven** — Cameras push events; no polling needed
- **Real-time Streaming** — Server-Sent Events (SSE) for instant dashboard alerts and status updates
- **Advanced Occupancy** — Atomic multi-zone tracking (B1, B2, Total) with floor transfer logic
- **Hybrid AI** — Camera smart events plus optional VA evidence/ReID validation
- **Phased Delivery** — Phase 2 components are pre-built and activated when ANPR cameras arrive
- **Camera Polling** — Instead of waiting for cameras to push HTTP webhooks (which requires network access from cameras to this machine), this service connects TO the cameras and listens on their alertStream endpoint in real-time.



## 📷 Camera Inventory

| Camera ID | Model | IP | Phase | Purpose |
|-----------|-------|----|-------|---------|
| CAM-01 | DS-2CD3681G2 | 192.168.1.101 | 1 | Field/Line detection |
| CAM-02 | DS-2CD3781G2 | 192.168.1.102 | 1 | Field/Line detection |
| CAM-03 | DS-2CD3783G2 (AcuSense) | 192.168.1.103 | 1 | Region entrance/exit |
| CAM-ENTRY | ANPR LPR | 192.168.1.104 | 2 | Entry gate |
| CAM-EXIT | ANPR LPR | 192.168.1.105 | 2 | Exit gate |

## 🚀 Quick Start

- SQL Server 2019+ (or Docker)
- Python 3.11+
- Hikvision cameras on local network

### 2. Setup
```bash
git clone <repo-url> && cd damanat-backend
python -m venv venv && source venv/bin/activate  # Linux/Mac
# Windows: python -m venv venv && venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # Edit with real IPs + DB URL
```

### 3. Start Database
```bash
# Option A: Docker (SQL Server Express or Developer)
docker-compose up -d sql-server

# Option B: Local SQL Server
# Ensure SQL Server Authentication is enabled
# Then: create database damanat_pms;
```

### 4. Initialize DB & Configure Cameras
```bash
python scripts/setup/init_db.py                      # Create DB tables
python scripts/test/test_camera_conn.py               # Verify cameras are reachable
python scripts/setup/configure_cameras.py --phase 1   # Register backend on cameras
```

### 5. Start Backend
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```
- **API docs**: http://192.168.1.50:8080/docs
- **ReDoc**: http://192.168.1.50:8080/redoc
- **Health**: http://192.168.1.50:8080/api/v1/health

## 🐳 Docker Deployment
```bash
# Start everything (DB + Backend)
docker-compose up -d
docker-compose up -d sql-server
docker-compose down -v
# View logs
docker-compose logs -f backend
```

## 📡 API Reference

| Phase | Method | Endpoint | Description |
|-------|--------|----------|-------------|
| Both | `GET` | `/api/v1/alerts/stream` | **Real-time Alert Stream (SSE)** — EventSource connection |
| Both | `POST` | `/api/v1/events/camera` | Camera webhook (all events) |
| Both | `GET` | `/api/v1/events` | Raw event log |
| Both | `GET` | `/api/v1/alerts` | Combined alerts (Intrusions/Violations/Capacity) |
| 1 | `GET` | `/api/v1/occupancy` | All zones occupancy (UC3) |
| 1 | `GET` | `/api/v1/occupancy/{zone_id}` | Single zone occupancy |
| 1 | `PUT` | `/api/v1/occupancy/{zone_id}/capacity` | Set zone capacity |
| 1 | `PUT` | `/api/v1/occupancy/{zone_id}/reset` | Reset zone count |
| 1 | `GET` | `/api/v1/violations` | Violation alerts (UC5) |
| 1 | `PUT` | `/api/v1/violations/{id}/resolve` | Resolve violation |
| 1 | `GET` | `/api/v1/intrusions` | Intrusion alerts (UC6) |
| 2 | `GET` | `/api/v1/entry-exit` | Entry/exit log (UC1) |
| 2 | `GET` | `/api/v1/entry-exit/count/today` | Today's count (UC1) |
| 2 | `GET` | `/api/v1/stats/parking-time` | Avg parking time (UC2) |
| 2 | `GET` | `/api/v1/stats/daily` | Daily summary (UC2) |
| 2 | `GET/POST/DELETE` | `/api/v1/vehicles` | Vehicle CRUD (UC4) |
| 2 | `GET` | `/api/v1/vehicles/lookup/{plate}` | Plate lookup (UC4) |
| 2 | `POST` | `/api/v1/internal/entry-confirmations` | Authenticated VA Entry V2 decision callback |
| Both | `GET` | `/api/v1/health` | System health check |

## Entry validation V2 rollout

Entry V2 sends in-memory vehicle crops from PMS-AI to Video Analytics (VA) and
accepts metadata-only decisions back. Configure the same `ENTRY_V2_MODE` and
`ENTRY_V2_SERVICE_KEY` on both services, and set `PMS_API_URL` here to the VA
origin. The modes are:

- `off` (default): legacy entry burst/FIFO processing only.
- `shadow`: send V2 evidence while legacy processing remains authoritative.
- `authoritative`: VA confirmations exclusively create entry logs and parking
  sessions; known exit ANPR events continue through the legacy exit flow.

In `shadow` and `authoritative`, PMS fails startup unless `PMS_API_URL` is a
plain credential-free absolute HTTP(S) URL and `ENTRY_V2_SERVICE_KEY` is set.
Transport timeouts must be positive and finite; image/pixel limits must be
positive and stay within VA's default envelope; crop padding must remain within
`0..0.5`. Authoritative mode additionally requires the MSSQL dialect because
its entry/session serialization uses SQL Server transaction-owned app locks.

PMS-AI posts multipart evidence to `/api/v2/entry-attempts` and
`/api/v2/entry-crossings`. Camera-provided vehicle crops are accepted, while
overview images normally require an exact camera rectangle and are cropped in
memory. For an inward CAM23/CAM03 crossing only, a missing rectangle falls back
to a visibly logged, bounded full frame so physical-entry evidence is not lost.
CAM23 uses `CAM23_ENTRY_LINE`/`CAM23_ENTRY_DIRECTION`; CAM03 reuses the existing
`ENTRY_CONFIRM_CAMERAS`, `ENTRY_CONFIRM_DIRECTIONS`,
`OCCUPANCY_ENTRANCE_ZONES`, and `FORWARD_DIRECTION_FIELD` calibration.
When CAM23 is enabled, authoritative mode refuses startup unless at least one
CAM23 line/direction filter is non-empty, and PMS applies every configured
filter before accepting any multipart image shape. Empty CAM23 filters remain
available only in `off`/`shadow` for calibration while legacy entry handling is
still authoritative. If both filters are configured, both must match.
PMS forwards CAM03's raw Hikvision line/direction when present (currently
`1`/`B-to-A`), while VA's local fallback zone emits canonical
`B1_Entrence`/`b-entry`; VA must allowlist both pairs before rollout.
ANPR overview images without a rectangle and all plate crops remain excluded.
Every outbound image is adaptively resized/JPEG-fitted to at most 12,000,000
decoded pixels, an 8,192-pixel side, and 4 MiB; PMS deterministically forwards at most
`ENTRY_V2_MAX_IMAGES` (default 4), matching VA's default intake contract. The
larger source is independently guarded by
`ENTRY_V2_MAX_SOURCE_IMAGE_BYTES` (default 16 MiB compressed) and
`ENTRY_V2_MAX_SOURCE_DECODED_PIXELS` (default 30,000,000 decoded pixels).
`ENTRY_V2_CAMERA_ALIASES` can resolve an
otherwise unknown source by exact `camera_id` or `device_serial`, but targets
are restricted to configured `CAM-ENTRY`/`CAM-EXIT` gate IDs. Alias only a
verified unique device serial; never alias a shared proxy/NAT peer IP. A crossing must
be an active vehicle event and carry the camera's raw direction; a calibrated
one-way line may instead be opted in through `ENTRY_V2_ONE_WAY_LINES`.

VA calls `/api/v1/internal/entry-confirmations` with `X-Service-Key`; the
callback does not accept image, base64, or path fields. Confirmed callbacks are
idempotent on normalized plate, entry camera, and captured time (with a narrow
SQL Server timestamp tolerance); `entry_captured_at` must include a timezone
offset and remain inside SQL Server's DATETIME range with room for that
tolerance. Confirmed callbacks atomically create the entry log and open
parking session without a snapshot. A confirmation arriving after a committed
exit receives a terminal `stale_after_exit` response and creates no rows.
A strictly validated re-entry closes any older open session at the crossing
time with `exit_camera_id=SYSTEM-REENTRY-RECONCILE`, then opens the new stay in
the same transaction; no synthetic exit log is fabricated. An older callback
arriving after an equal/newer open stay receives terminal
`superseded_by_newer_entry`, so VA drops its pending callback without publishing
that stale identity.

`zone_occupancy.current_count` is reconciled from existing open
`ParkingSession` rows after entry, exit, and configured line events. A line event
is only a reconciliation trigger; it never adds or subtracts a count, so duplicate
or missed crossings cannot accumulate drift beyond the session source of truth.

In authoritative mode, VA network/capacity/configuration failures, redirects,
or a VA response whose reported mode is not `authoritative` produce HTTP 503 to
the camera so it can retry. Invalid evidence is acknowledged without creating
an entry, preventing a permanent bad payload from causing a retry loop.

## 🧪 Testing

### Run All Simulations (Full Demo Data)
```bash
python scripts/test/run_all_simulations.py
```

### Run Unit Tests
```bash
python -m pytest tests/ -v
```

### Simulate Specific Events
```bash
# UC3 — Occupancy
python scripts/test/simulate_event.py --event regionEntrance --zone 1 --ip 10.1.13.63
python scripts/test/simulate_event.py --event regionExiting  --zone 1 --ip 10.1.13.63
```
python scripts/test/simulate_event.py --event regionExiting  --zone parking-row-A --ip 192.168.1.103

# UC5 — Violation
python scripts/test/simulate_event.py --event fielddetection --zone restricted-vip --target vehicle --ip 192.168.1.101

# UC6 — Intrusion
python scripts/test/simulate_event.py --event fielddetection --zone emergency-exit --target vehicle --ip 192.168.1.102

# Phase 2 — ANPR
python scripts/test/simulate_event.py --event anpr --plate ABC-1234 --ip 192.168.1.104
```

## 🏢 Project Structure

```
damanat-backend/
├── app/
│   ├── main.py                 # FastAPI app entry point
│   ├── config.py               # Pydantic settings
│   ├── database.py             # SQLAlchemy engine + session
│   ├── models/                 # SQLAlchemy ORM models
│   │   ├── camera_event.py     # Raw event log
│   │   ├── zone_occupancy.py   # Per-zone occupancy state
│   │   ├── alert.py            # All alert types
│   │   ├── vehicle.py          # 🔜 Phase 2: Registered vehicles
│   │   └── entry_exit_log.py   # 🔜 Phase 2: Entry/exit records
│   ├── schemas/                # Pydantic request/response schemas
│   ├── services/               # Business logic
│   │   ├── event_parser.py     # XML/JSON → ParsedCameraEvent
│   │   ├── event_dispatcher.py # Route events to handlers
│   │   ├── occupancy_service.py # UC3
│   │   ├── violation_service.py # UC5
│   │   ├── intrusion_service.py # UC6
│   │   ├── alert_service.py    # Shared alert creator
│   │   ├── entry_exit_service.py # 🔜 Phase 2: UC1+UC2+UC4
│   │   └── vehicle_service.py  # 🔜 Phase 2: Vehicle lookup
│   ├── routers/                # API endpoints
│   └── utils/                  # Logger, XML/JSON, and EventBus (SSE) helpers
│       └── event_bus.py        # 🔔 Internal event broadcasting for SSE
├── scripts/
│   ├── setup/                  # Camera + DB configuration
│   └── test/                   # Event simulation + connectivity
├── tests/                      # Unit tests
├── logs/                       # Application logs (auto-created)
├── docker-compose.yml          # SQL Server + Backend
├── Dockerfile                  # Container build
├── requirements.txt            # Python dependencies
└── .env.example                # Environment template
```

## 🔜 Phase 2 Activation

When ANPR cameras are physically installed:

1. Update IP addresses in `.env` for `CAM-ENTRY` and `CAM-EXIT`
2. Run `python scripts/setup/configure_cameras.py --phase 2`
3. Run `python scripts/setup/configure_anpr_cameras.py`
4. Uncomment Phase 2 router imports in `app/main.py` (3 lines)
5. Import vehicle data via `POST /api/v1/vehicles`
6. Test with: `python scripts/test/simulate_event.py --event anpr --plate ABC-1234 --ip 192.168.1.104`

## 🔒 Security

- **API Key Auth**: Set `API_KEY` in `.env` to enable API key authentication
  - Camera webhook (`/events/camera`) and health endpoints bypass this general key
  - Pass key via `X-API-Key` header or `?api_key=` query param
- **Camera Webhook Transport**: `CAMERA_EVENT_MAX_BODY_BYTES` bounds fixed and
  chunked payloads while they are read. Set
  `CAMERA_EVENT_ALLOWED_SOURCE_CIDRS` to the direct camera and gateway IPs/CIDRs
  in production. Empty is backward-compatible only in `off`/`shadow` mode;
  authoritative mode fails configuration/startup and rejects requests when the
  allowlist is empty. Invalid CIDRs fail configuration in every mode. The check
  uses the effective connection peer, not an application-parsed
  `X-Forwarded-For` header. Behind a reverse proxy, either allowlist that proxy
  peer or configure the ASGI server's trusted proxy sources narrowly before
  relying on forwarded client addresses.
- **Entry V2 Service Auth**: `/api/v1/internal/entry-confirmations` bypasses the
  general API key only so it can enforce its own `X-Service-Key` credential.
- **PMS → VA Auth**: PMS-AI sends the same `ENTRY_V2_SERVICE_KEY` to VA's ANPR
  and V2 intake routes. VA checks it before reading request bodies in V2 modes.
  Active-V2 auth/routing/backpressure failures retain exit plate/direction/source
  time in a metadata-only delivery spool rather than silently discarding the
  exit; those exit records do not age out. A 2xx counts as delivered only when
  VA returns matching `status`, plate, direction, and source timestamp fields.
  Spool creation fsyncs file content and the atomic rename; malformed records
  are moved to visible `.corrupt` quarantine files, while transient read errors
  retain the original. Unexpected authoritative processing or commit failures
  return HTTP 503 with `Retry-After` so the camera retries.
  If both live exit delivery and the spool write fail (for example, disk full),
  authoritative also returns 503; shadow preserves its legacy acknowledgement.
- **Camera Auth**: Uses HTTP Digest authentication for ISAPI calls
- **No Internet**: System operates entirely on LAN

## 📝 Code Standards

- Always use `get_logger(__name__)` — no `print()` in production code
- Never hardcode IPs — use `settings` from `config.py`
- Always add docstrings explaining: Purpose, Camera, Event type
- Camera webhooks return HTTP 200 in legacy/shadow mode; authoritative Entry V2
  returns HTTP 503 for retryable VA delivery failures.
- **SQL Server Note**: Always specify a length for `String()` columns that are used in indexes or unique constraints (e.g., `String(255)`), as SQL Server does not support indexing columns with unlimited length.
- Phase 2 files: full working implementations, commented out in `main.py`

## 📚 Full Documentation

See [`Docs/DAMANAT-SYSTEM-PROMPT.md`](../Docs/DAMANAT-SYSTEM-PROMPT.md) for complete system documentation.

---

*Damanat Parking Analytics — Spectech Project*  
*Phase 1: DS-2CD3681G2 + DS-2CD3781G2 + DS-2CD3783G2 (AcuSense)*  
*Phase 2: ANPR LPR cameras at entry/exit gates*
