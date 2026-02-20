# 👥 Damanat Project — Team Work Split
> **Sprint Duration:** 5 Days  
> **Team Size:** 3 Developers  
> **Mode:** Fully Offline (LAN), FastAPI + PostgreSQL + Hikvision ISAPI  
> **Reference:** See `DAMANAT-SYSTEM-PROMPT.md` for full technical details

---

## 👤 Team Roles

| Person | Role | Location | Focus |
|--------|------|----------|-------|
| **Ahmed** (You) | Team Lead | 🏢 On-site (Client) | Camera setup, integration testing, client liaison |
| **Dev 1** | Backend Engineer | 🖥️ Office/Remote | Event ingestion, database, infrastructure |
| **Dev 2** | Use Case Engineer | 🖥️ Office/Remote | Business logic, REST API endpoints, alerts |

---

## 📅 5-Day Sprint Plan

---

### 🗓️ Day 1 — Foundation & Setup

#### Ahmed (On-site)
- [ ] Get physical access to all 3 cameras and confirm their IP addresses
- [ ] Update `CAMERAS` config in `app/config.py` with real IPs and credentials
- [ ] Confirm backend server IP on the local network
- [ ] Run `scripts/setup/configure_cameras.py` to register backend as HTTP push target on all cameras
- [ ] Open camera web UIs (`http://{camera_ip}`) and verify you can log in
- [ ] On **CAM-03 (DS-2CD3783G2)**: Draw initial zones/regions via camera web UI:
  - Parking rows → for `regionEntrance` / `regionExiting` (UC3)
  - Restricted areas → for `fielddetection` violation alerts (UC5)
- [ ] On **CAM-01 & CAM-02**: Enable intrusion detection zones via camera web UI
- [ ] Send first test event from camera to confirm it reaches backend (coordinate with Dev 1)
- [ ] Daily sync call with Dev 1 & Dev 2 at end of day

#### Dev 1 (Backend Engineer)
- [ ] Create GitHub repo, set up branch structure (`main` / `dev` / feature branches)
- [ ] Scaffold FastAPI project from `DAMANAT-SYSTEM-PROMPT.md` folder structure
- [ ] Set up `requirements.txt` and virtual environment
- [ ] Set up PostgreSQL locally (or Docker)
- [ ] Implement `app/database.py` (connection, session, `create_tables()`)
- [ ] Implement all DB models: `camera_event.py`, `zone_occupancy.py`, `alert.py`
- [ ] Implement `app/main.py` with router placeholders
- [ ] Implement `POST /api/v1/events/camera` webhook receiver (just logs raw XML for now)
- [ ] Make sure backend is reachable at `http://{BACKEND_IP}:8080/api/v1/events/camera`
- [ ] Share server IP with Ahmed so cameras can be pointed to it

#### Dev 2 (Use Case Engineer)
- [ ] Clone repo and set up local environment
- [ ] Read `DAMANAT-SYSTEM-PROMPT.md` fully — understand event types and use cases
- [ ] Implement `app/services/event_parser.py` (parse camera XML → `ParsedCameraEvent`)
- [ ] Implement `app/utils/xml_parser.py` helpers
- [ ] Implement `app/utils/logger.py`
- [ ] Write unit tests for XML parser using sample payloads from `DAMANAT-SYSTEM-PROMPT.md`
- [ ] Implement `scripts/test/simulate_event.py` (test event sender)

---

### 🗓️ Day 2 — Core Event Pipeline

#### Ahmed (On-site)
- [ ] Verify all 3 cameras are pushing events successfully to backend (check with Dev 1)
- [ ] Fine-tune zone polygons on cameras based on actual parking layout:
  - CAM-03: adjust `regionEntrance` / `regionExiting` zones to match real parking rows
  - CAM-01 & CAM-02: set `fielddetection` zones for intrusion coverage
- [ ] Capture real event XMLs from cameras and share with Dev 1 & Dev 2 (for accurate parsing)
- [ ] Confirm `detectionTarget` = `"vehicle"` is being returned correctly in events
- [ ] Test: walk in front of CAM-01 (human) → confirm no false alert (vehicle filter working)
- [ ] Test: drive/move vehicle in CAM-03 zone → confirm event is received

#### Dev 1 (Backend Engineer)
- [ ] Integrate `event_parser.py` into the webhook receiver (`routers/events.py`)
- [ ] Implement `app/services/event_dispatcher.py` (routes events to correct handler)
- [ ] Save parsed events to `camera_events` DB table
- [ ] Implement `GET /api/v1/events` endpoint (list recent events)
- [ ] Implement `GET /api/v1/health` endpoint
- [ ] Test full flow: `simulate_event.py` → webhook → parse → save to DB → verify in DB
- [ ] Deploy backend to the shared LAN server so Ahmed can test with real cameras

#### Dev 2 (Use Case Engineer)
- [ ] Implement `app/services/occupancy_service.py` (UC3)
- [ ] Implement `app/routers/occupancy.py` (UC3 endpoints)
- [ ] Implement `app/services/alert_service.py` (shared alert creator)
- [ ] Register `occupancy.py` router in `app/main.py`
- [ ] Test UC3: simulate `regionEntrance` × 3 → confirm count goes up, simulate `regionExiting` → confirm count goes down
- [ ] Test occupancy alert: fill zone to 90%+ → confirm alert is created

---

### 🗓️ Day 3 — Use Case Logic

#### Ahmed (On-site)
- [ ] Configure restricted zones on cameras for violation use case (UC5):
  - Label zones clearly: `"restricted-vip"`, `"no-parking-zone"`, `"emergency-exit"` etc.
  - Match zone names with those defined in `violation_service.py` `RESTRICTED_ZONES`
- [ ] Configure intrusion zones on cameras for UC6:
  - Match with `MONITORED_INTRUSION_ZONES` in `intrusion_service.py`
- [ ] Run end-to-end test for UC3 with real vehicle movement
- [ ] Document real zone IDs (from camera) and share with Dev 2 to update config
- [ ] Test: trigger violation by entering a restricted zone → verify alert appears

#### Dev 1 (Backend Engineer)
- [ ] Harden the webhook receiver:
  - Handle malformed XML gracefully
  - Always return HTTP 200 to camera (prevent retry storms)
  - Add request logging (IP, timestamp, payload size)
- [ ] Implement event deduplication logic
- [ ] Add DB indexing for common query patterns
- [ ] Implement `scripts/setup/init_db.py`
- [ ] Write integration test: full pipeline from raw XML to DB entry

#### Dev 2 (Use Case Engineer)
- [ ] Implement `app/services/violation_service.py` (UC5)
- [ ] Implement `app/routers/violations.py` (UC5 endpoints)
- [ ] Implement `app/services/intrusion_service.py` (UC6)
- [ ] Implement `app/routers/intrusion.py` (UC6 endpoints)
- [ ] Register all new routers in `app/main.py`
- [ ] Test UC5: simulate `fielddetection` on restricted zone → confirm violation alert
- [ ] Test UC6: simulate `fielddetection` on monitored zone → confirm intrusion alert
- [ ] Implement `PUT /api/v1/violations/{id}/resolve` endpoint

---

### 🗓️ Day 4 — Integration & Polish

#### Ahmed (On-site)
- [ ] Full live test with real cameras for all 3 use cases:
  - UC3: vehicle entering/leaving parking zone → occupancy updates
  - UC5: vehicle entering restricted area → violation alert
  - UC6: vehicle in monitored zone → intrusion alert
- [ ] Collect feedback: are zone sizes correct? Are false alerts happening?
- [ ] Adjust zone polygons on cameras if needed based on test results
- [ ] Verify alert cooldown is working (no alert storms for same zone)
- [ ] Confirm all 3 camera IPs are stable (consider setting static IPs if not already)
- [ ] Prepare a brief demo script for client review on Day 5

#### Dev 1 (Backend Engineer)
- [ ] Run full integration test with all 3 cameras connected
- [ ] Performance check: backend handles burst of events without dropping any
- [ ] Implement basic logging to file (`logs/events.log`)
- [ ] Write `README.md` with startup instructions
- [ ] Final code review of event pipeline
- [ ] Merge `feature/dev1-*` to `dev` branch

#### Dev 2 (Use Case Engineer)
- [ ] Fix any issues found during Ahmed's live tests
- [ ] Update `RESTRICTED_ZONES` and `MONITORED_INTRUSION_ZONES` with real zone IDs from Ahmed
- [ ] Add `GET /api/v1/alerts` combined endpoint (all alerts, filterable by type)
- [ ] Write final unit tests for all services
- [ ] Merge `feature/dev2-*` to `dev` branch
- [ ] Verify Swagger UI at `/docs` looks clean and all endpoints are documented

---

### 🗓️ Day 5 — Testing, Deploy & Handover

#### Ahmed (On-site)
- [ ] Run complete end-to-end system test (all cameras, all use cases)
- [ ] Present working system to client / stakeholders
- [ ] Collect client feedback and document any change requests
- [ ] Confirm system is stable and running on LAN server
- [ ] Take note of any Phase 2 items (ANPR cameras, entry/exit counting)
- [ ] Sign off on Day 5 delivery

#### Dev 1 (Backend Engineer)
- [ ] Final merge: `dev` → `main`
- [ ] Verify backend starts cleanly on the production LAN server
- [ ] Set up systemd service or PM2 so backend auto-starts on reboot
- [ ] Final smoke test: hit all endpoints via Swagger UI
- [ ] Archive camera config script output as documentation

#### Dev 2 (Use Case Engineer)
- [ ] Write short `HANDOVER.md` for client / future devs:
  - How to add a new camera
  - How to add a new zone
  - How to change capacity for a zone
  - How to add a new restricted zone
- [ ] Package all test scripts cleanly in `scripts/test/`
- [ ] Final QA pass on all API responses

---

## 📊 Responsibility Matrix

| Task | Ahmed | Dev 1 | Dev 2 |
|------|:-----:|:-----:|:-----:|
| Camera physical setup | ✅ | | |
| Camera zone configuration | ✅ | | |
| Camera HTTP push config (`configure_cameras.py`) | ✅ | | |
| On-site live testing | ✅ | | |
| Client communication | ✅ | | |
| FastAPI project scaffold | | ✅ | |
| Database models & migrations | | ✅ | |
| Webhook receiver (`POST /events/camera`) | | ✅ | |
| Event parser & dispatcher | | ✅ | ✅ |
| Event storage & deduplication | | ✅ | |
| UC3 — Occupancy service | | | ✅ |
| UC5 — Violation service | | | ✅ |
| UC6 — Intrusion service | | | ✅ |
| Alert service (shared) | | | ✅ |
| REST API endpoints | | | ✅ |
| Unit & integration tests | | ✅ | ✅ |
| Deployment & auto-start | | ✅ | |
| Handover documentation | | | ✅ |

---

## 🔗 Communication & Sync

| When | What |
|------|------|
| **Start of each day** | 15-min standup: what did you do yesterday? what today? any blockers? |
| **Real-time (always)** | WhatsApp/Telegram group for quick questions and sharing logs/screenshots |
| **Ahmed → Devs** | Share camera event XML samples as soon as cameras are live (Day 1) |
| **Ahmed → Dev 2** | Share real zone IDs from cameras on Day 3 to update config |
| **End of Day 2** | Check: can backend receive real events from real cameras? If not — STOP and fix |
| **End of Day 4** | Merge everything to `dev`, full test on shared server |
| **Day 5** | Deploy to production LAN server, present to client |

---

## ⚠️ Critical Dependencies (Don't Miss These!)

```
Ahmed confirms camera IPs  →  Dev 1 updates config.py      (needed: Day 1)
Backend server is live     →  Ahmed points cameras to it    (needed: Day 1)
Real XML samples shared    →  Dev 2 validates event parser  (needed: Day 2)
Real zone IDs from cameras →  Dev 2 updates RESTRICTED_ZONES (needed: Day 3)
```

---

## 🚦 Definition of Done (Day 5)

- [ ] All 3 cameras push events to backend with zero manual polling
- [ ] `POST /api/v1/events/camera` receives and logs all event types
- [ ] `GET /api/v1/occupancy` returns live vehicle counts per zone
- [ ] `GET /api/v1/violations` returns active violation alerts
- [ ] `GET /api/v1/intrusions` returns active intrusion alerts
- [ ] Backend survives camera restart (reconnects automatically)
- [ ] Backend auto-starts on server reboot
- [ ] All code is on `main` branch
- [ ] `README.md` and `HANDOVER.md` are complete

---

## 📦 Phase 2 Backlog (Out of Scope This Sprint)

| Item | Requires |
|------|---------|
| UC1: Entry/Exit Counting | ANPR camera (e.g. DS-2CD7A26G0/P) |
| UC2: Parking Time & Daily Count | ANPR camera + UC1 data |
| UC4: Full Vehicle ID by Plate | ANPR camera |
| Dashboard UI | Frontend developer / sprint |
| Push notifications (SMS/email) | `alert_service.py` extension |
| Multi-site support | Config extension |

---

*Document prepared by Clawdy — Damanat Project — February 2026*
