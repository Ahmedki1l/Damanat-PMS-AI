# Damanat Parking Analytics Backend

Fully offline AI camera event processing system for Damanat parking facility (Saudi Arabia).

> **Phase 1**: Intrusion Detection, Parking Occupancy, Violation Alerts  
> **Phase 2**: ANPR Entry/Exit Counting, Parking Duration, Vehicle ID

---

## 🏗️ Architecture

```
Edge AI Cameras (Hikvision) → HTTP Push (LAN) → FastAPI Backend → PostgreSQL → Dashboard & Alerts
```

- **Fully Offline** — LAN only, no cloud or internet required
- **Event-Driven** — Cameras push events; no polling needed
- **No Backend AI** — All AI processing on camera edge; backend reacts to events
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

### 1. Prerequisites
- Python 3.11+
- PostgreSQL 14+ (or Docker)
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
# Option A: Docker (recommended)
docker-compose up -d db

# Option B: Local PostgreSQL
# Windows: Start PostgreSQL service
# Linux: sudo systemctl start postgresql
# Then: createdb damanat_db
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

# View logs
docker-compose logs -f backend
```

## 📡 API Endpoints

| Phase | Method | Endpoint | Description |
|-------|--------|----------|-------------|
| Both | `POST` | `/api/v1/events/camera` | Camera webhook (all events) |
| Both | `GET` | `/api/v1/events` | Raw event log |
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
| Both | `GET` | `/api/v1/health` | System health check |

## 🧪 Testing

### Run Unit Tests
```bash
python -m pytest tests/ -v
```

### Simulate Camera Events
```bash
# UC3 — Occupancy
python scripts/test/simulate_event.py --event regionEntrance --zone parking-row-A --ip 192.168.1.103
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
│   └── utils/                  # Logger, XML/JSON helpers
├── scripts/
│   ├── setup/                  # Camera + DB configuration
│   └── test/                   # Event simulation + connectivity
├── tests/                      # Unit tests
├── logs/                       # Application logs (auto-created)
├── docker-compose.yml          # PostgreSQL + Backend
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
  - Camera webhook (`/events/camera`) and health endpoints are always open
  - Pass key via `X-API-Key` header or `?api_key=` query param
- **Camera Auth**: Uses HTTP Digest authentication for ISAPI calls
- **No Internet**: System operates entirely on LAN

## 📝 Code Standards

- Always use `get_logger(__name__)` — no `print()` in production code
- Never hardcode IPs — use `settings` from `config.py`
- Always add docstrings explaining: Purpose, Camera, Event type
- Always return HTTP 200 from the camera webhook
- Phase 2 files: full working implementations, commented out in `main.py`

## 📚 Full Documentation

See [`Docs/DAMANAT-SYSTEM-PROMPT.md`](../Docs/DAMANAT-SYSTEM-PROMPT.md) for complete system documentation.

---

*Damanat Parking Analytics — Spectech Project*  
*Phase 1: DS-2CD3681G2 + DS-2CD3781G2 + DS-2CD3783G2 (AcuSense)*  
*Phase 2: ANPR LPR cameras at entry/exit gates*
