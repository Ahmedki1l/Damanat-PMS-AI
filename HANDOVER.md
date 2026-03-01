# Damanat Parking Analytics — Handover Guide

> For the client and any future developer working on this project.  
> Read this file before making any changes to the system.

---

## System Overview

Hikvision AI cameras detect events and push them automatically to the backend via HTTP POST.  
The system runs **fully offline** — no internet required, everything on LAN.

```
Hikvision Camera → HTTP POST (XML) → FastAPI Backend (port 8080) → PostgreSQL → API / Alerts
```

---

## Cameras — Phase 1 (Active)

| ID | Role |
|----|------|
| CAM-02 | Parking zone coverage |
| CAM-04 | Parking zone coverage |
| CAM-35 | Parking zone coverage |

> **Credentials (IP, username, password) are stored in `.env` only — never in code or Git.**

## Cameras — Phase 2 (Not yet installed)

| ID | Role |
|----|------|
| CAM-ENTRY | Entry gate — ANPR plate reading |
| CAM-EXIT | Exit gate — ANPR plate reading |

> IPs and passwords to be added to `.env` when cameras are physically installed.

---

## Backend

| Setting | Value |
|---------|-------|
| Port | `8080` |
| Backend IP | See `BACKEND_IP` in `.env` |
| API Docs (Swagger) | `http://{BACKEND_IP}:8080/docs` |
| Local dev docs | `http://127.0.0.1:8080/docs` |

---

## Starting the System

```bash
# 1. Activate virtual environment
venv\Scripts\activate        # Windows
source venv/bin/activate     # Linux/Mac

# 2. Start the backend
uvicorn app.main:app --host 0.0.0.0 --port 8080

# 3. Verify it's running
# Open in browser: http://127.0.0.1:8080/docs
```

---

## How to Add a New Camera

**Step 1 — Add credentials to `.env`:**
```env
CAM_NEW_IP=10.1.13.XX
CAM_NEW_PASSWORD=your_password
```

**Step 2 — Register the camera in `app/config.py`:**
```python
# Add the IP and password fields:
CAM_NEW_IP: str = "0.0.0.0"
CAM_NEW_PASSWORD: str = "CHANGE_ME"

# Add to the CAMERAS property:
"CAM-NEW": {"ip": self.CAM_NEW_IP, "user": self.CAMERA_USER, "password": self.CAM_NEW_PASSWORD, "phase": 1},

# Add to the CAMERA_IP_MAP property:
self.CAM_NEW_IP: "CAM-NEW",
```

**Step 3 — Configure the camera to push events to the backend (run on-site):**
```bash
python scripts/setup/configure_cameras.py --phase 1
```

No other code changes needed. The camera will start sending events automatically.

---

## How to Add a New Parking Zone

**Step 1 — Draw the zone on the camera web UI:**
- Open `http://{camera_ip}` in browser and login with camera credentials from `.env`
- Navigate to: Configuration → Smart Event → Region Entrance/Exiting
- Draw the zone polygon and give it a name — e.g. `parking-row-D`
- Note the exact name — you will need it in Step 3

**Step 2 — No code changes needed.**  
The zone is created automatically in the database when the first event arrives from it.

**Step 3 — Set zone capacity via API:**
```
PUT http://127.0.0.1:8080/api/v1/occupancy/parking-row-D/capacity
Content-Type: application/json
Body: {"max_capacity": 20}
```

Or use Swagger: `http://127.0.0.1:8080/docs` → `PUT /occupancy/{zone_id}/capacity` → Try it out

> **Default capacity** if not set: `10` vehicles per zone.

---

## How to Change Zone Capacity

**Via API (no restart needed):**
```
PUT http://127.0.0.1:8080/api/v1/occupancy/{zone_id}/capacity
Content-Type: application/json
Body: {"max_capacity": 25}
```

> Capacity alert fires automatically at **90%** occupancy.

---

## How to Add a New Restricted Zone (UC5 — Violations)

Restricted zones trigger a violation alert when a vehicle is detected inside them.

**Step 1 — Draw the detection zone on the camera and note the exact name.**

**Step 2 — Add the zone name to `app/services/violation_service.py`:**
```python
RESTRICTED_ZONES = {
    "restricted-vip",
    "no-parking-zone",
    "emergency-exit",
    "loading-bay",
    "your-new-zone-name",    # ← add here
}
```

**Step 3 — Restart the backend.**

---

## How to Add a New Monitored Intrusion Zone (UC6 — Intrusion)

**Edit `app/services/intrusion_service.py`:**
```python
MONITORED_INTRUSION_ZONES = {
    "emergency-exit",
    "staff-only-area",
    "after-hours-zone",
    "your-new-zone-name",    # ← add here
}
```

**Restart the backend.**

---

## API Endpoints Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/events/camera` | Camera webhook — do not call manually |
| GET | `/api/v1/events` | Raw event log |
| GET | `/api/v1/occupancy` | Live vehicle count for all zones (UC3) |
| GET | `/api/v1/occupancy/{zone_id}` | Single zone occupancy |
| PUT | `/api/v1/occupancy/{zone_id}/capacity` | Update zone capacity |
| PUT | `/api/v1/occupancy/{zone_id}/reset` | Reset zone count to 0 |
| GET | `/api/v1/violations` | Violation alerts (UC5) |
| PUT | `/api/v1/violations/{id}/resolve` | Mark violation as resolved |
| GET | `/api/v1/intrusions` | Intrusion alerts (UC6) |
| GET | `/api/v1/alerts` | All alerts — filterable by `alert_type` and `is_resolved` |
| GET | `/api/v1/health` | System health — backend, database, cameras |

**Full interactive docs:** `http://127.0.0.1:8080/docs`

---

## Testing Without Real Cameras

```bash
# UC3 — vehicle enters a parking zone
python scripts/test/simulate_event.py --event regionEntrance --zone parking-row-A

# UC3 — vehicle exits a parking zone
python scripts/test/simulate_event.py --event regionExiting --zone parking-row-A

# UC5 — violation in restricted zone
python scripts/test/simulate_event.py --event fielddetection --zone restricted-vip

# UC6 — intrusion in monitored zone
python scripts/test/simulate_event.py --event fielddetection --zone emergency-exit
```

---

## Activating Phase 2 (ANPR cameras — when installed)

When ANPR cameras are physically installed at the entry and exit gates:

1. Add camera IPs and passwords to `.env`:
   ```env
   CAM_ENTRY_IP=...
   CAM_ENTRY_PASSWORD=...
   CAM_EXIT_IP=...
   CAM_EXIT_PASSWORD=...
   ```
2. Uncomment `CAM-ENTRY` and `CAM-EXIT` lines in `app/config.py` (CAMERAS and CAMERA_IP_MAP)
3. Run: `python scripts/setup/configure_cameras.py --phase 2`
4. Run: `python scripts/setup/configure_anpr_cameras.py`
5. Uncomment Phase 2 routers in `app/main.py` (3 lines marked `# Phase 2`)
6. Import vehicle data via `POST /api/v1/vehicles`
7. Test: `python scripts/test/simulate_event.py --event anpr --plate ABC-1234`

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| No events arriving from cameras | Verify camera HTTP push is configured to backend IP:8080 — run `configure_cameras.py` |
| Zone vehicle count is wrong | Use `PUT /api/v1/occupancy/{zone_id}/reset` |
| Camera appears as `UNKNOWN-{ip}` | Add camera IP to `CAMERA_IP_MAP` in `config.py` |
| Backend fails to start | Check `DATABASE_URL` in `.env` and confirm PostgreSQL is running |
| Camera unreachable | Run `python scripts/test/test_camera_conn.py` |
| View all logs | Open `logs/events.log` |

---

## .env Structure (do not commit to Git)

```env
# Database
DATABASE_URL=postgresql://damanat:damanat@localhost:5432/damanat_db

# Network
BACKEND_IP=...
BACKEND_PORT=8080

# Camera credentials — fill with real values
CAMERA_USER=...
CAM_02_IP=...
CAM_02_PASSWORD=...
CAM_04_IP=...
CAM_04_PASSWORD=...
CAM_35_IP=...
CAM_35_PASSWORD=...

# Phase 2 — add when ANPR cameras are installed
# CAM_ENTRY_IP=
# CAM_ENTRY_PASSWORD=
# CAM_EXIT_IP=
# CAM_EXIT_PASSWORD=

# Optional API key auth (leave empty to disable)
API_KEY=
```

> Make sure `.env` is listed in `.gitignore` — **never commit it to Git.**

---

*Damanat Parking Analytics — Spectech*  
*Phase 1: CAM-02, CAM-04, CAM-35 | Backend port: 8080 | Fully Offline LAN*
