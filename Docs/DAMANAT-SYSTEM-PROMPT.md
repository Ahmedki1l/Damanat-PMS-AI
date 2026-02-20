# 🏗️ Damanat Parking Analytics System — Full Technical Guide
> **For:** Antigravity AI Model / Development Team  
> **Project:** Damanat Parking Facility — AI Camera Analytics (Saudi Arabia)  
> **Architecture:** Edge AI Cameras → HTTP Push → FastAPI Backend → Dashboard & Alerts  
> **Mode:** Fully Offline (LAN only, no cloud, no internet required)  
> **Stack:** Python 3.11+, FastAPI, PostgreSQL, Hikvision ISAPI  
> **Phases:** Phase 1 (current cameras) + Phase 2 (ANPR cameras) — both fully documented  

---

## 📋 Table of Contents

1. [System Overview](#1-system-overview)
2. [Camera Inventory](#2-camera-inventory)
3. [How Camera Notifications Work](#3-how-camera-notifications-work)
4. [Configuring Camera HTTP Push (Webhook)](#4-configuring-camera-http-push-webhook)
5. [FastAPI Project Structure](#5-fastapi-project-structure)
6. [Database Schema](#6-database-schema)
7. [Use Case Scripts — Phase 1](#7-use-case-scripts--phase-1)
8. [Use Case Scripts — Phase 2 (ANPR)](#8-use-case-scripts--phase-2-anpr)
9. [Team Guidelines](#9-team-guidelines)
10. [Testing & Validation](#10-testing--validation)

---

## 1. System Overview

### Phase 1 Architecture (Current)
```
┌─────────────────────────────────────────────────────────────────┐
│                       LOCAL NETWORK (LAN)                       │
│                                                                 │
│  DS-2CD3681G2 (192.168.1.101) ──┐                              │
│  DS-2CD3781G2 (192.168.1.102) ──┼──HTTP POST──► FastAPI        │
│  DS-2CD3783G2 (192.168.1.103) ──┘    Events    Backend         │
│                                               (192.168.1.50)   │
│                                                    │            │
│                                              PostgreSQL DB      │
│                                                    │            │
│                                           Dashboard / Alerts    │
└─────────────────────────────────────────────────────────────────┘
```

### Phase 2 Architecture (After ANPR Camera Addition)
```
┌─────────────────────────────────────────────────────────────────┐
│                       LOCAL NETWORK (LAN)                       │
│                                                                 │
│  DS-2CD3681G2  (192.168.1.101) ──┐                             │
│  DS-2CD3781G2  (192.168.1.102) ──┤                             │
│  DS-2CD3783G2  (192.168.1.103) ──┼──HTTP POST──► FastAPI       │
│  ANPR-ENTRY    (192.168.1.104) ──┤    Events    Backend        │
│  ANPR-EXIT     (192.168.1.105) ──┘              (192.168.1.50) │
│                                                      │          │
│                                                PostgreSQL DB    │
│                                                      │          │
│                                             Dashboard / Alerts  │
└─────────────────────────────────────────────────────────────────┘
```

### Key Design Principles
- **Event-driven**: Cameras push events to backend — zero polling
- **Fully offline**: All LAN communication, no internet required
- **No backend AI**: All AI runs on camera edge; backend only reacts to events
- **Phased delivery**: System is built Phase 1 → Phase 2 with no breaking changes
- **Single entry point**: All camera events hit the same webhook endpoint regardless of phase

### Use Case Coverage by Phase

| # | Use Case | Phase | Camera(s) | Event(s) Used |
|---|----------|-------|-----------|---------------|
| UC3 | Parking Occupancy Monitoring | ✅ Phase 1 | DS-2CD3783G2 | `regionEntrance`, `regionExiting` |
| UC4 | Vehicle Presence (basic) | ✅ Phase 1 | All 3 | `fielddetection` with `detectionTarget=vehicle` |
| UC5 | Proactive Violation Alerts | ✅ Phase 1 | All 3 | `fielddetection`, `linedetection`, `regionEntrance` |
| UC6 | Intrusion Detection | ✅ Phase 1 | All 3 | `fielddetection`, `regionEntrance` |
| UC1 | Entry & Exit Counting | 🔜 Phase 2 | ANPR cameras | `AccessControllerEvent` |
| UC2 | Avg Parking Time & Daily Count | 🔜 Phase 2 | ANPR cameras | `AccessControllerEvent` |
| UC4+ | Full Vehicle ID by Plate | 🔜 Phase 2 | ANPR cameras | `AccessControllerEvent` (cardNo = plate) |

---

## 2. Camera Inventory

### Phase 1 — Current Cameras

| ID | Model | IP | Type | AI Level | Role |
|----|-------|----|------|----------|------|
| CAM-01 | DS-2CD3681G2-LIZSU | 192.168.1.101 | Bullet | Basic | Outdoor perimeter / parking lot |
| CAM-02 | DS-2CD3781G2-LIZSU | 192.168.1.102 | Dome | Basic | Indoor / covered parking |
| CAM-03 | DS-2CD3783G2-IZSU | 192.168.1.103 | AcuSense Dome | Deep Learning | Critical zones, occupancy, violations |

### Phase 2 — ANPR Cameras to Add

| ID | Recommended Model | IP | Type | Role |
|----|------------------|----|------|------|
| CAM-ENTRY | Hikvision DS-2CD7A26G0/P-IZS (or equivalent LPR) | 192.168.1.104 | ANPR/LPR | Entry gate — captures plate on entry |
| CAM-EXIT | Hikvision DS-2CD7A26G0/P-IZS (or equivalent LPR) | 192.168.1.105 | ANPR/LPR | Exit gate — captures plate on exit |

> **ANPR Camera Requirements:** Must support ISAPI + ANPR/LPR feature + `AccessControllerEvent` push.  
> Confirm with supplier that the model supports `cardNo` field in event payload (= license plate).

### Camera Credentials (update in `.env` before use)
```python
CAMERAS = {
    # Phase 1
    "CAM-01": {"ip": "192.168.1.101", "user": "admin", "password": "CHANGE_ME", "phase": 1},
    "CAM-02": {"ip": "192.168.1.102", "user": "admin", "password": "CHANGE_ME", "phase": 1},
    "CAM-03": {"ip": "192.168.1.103", "user": "admin", "password": "CHANGE_ME", "phase": 1},
    # Phase 2 (add when cameras are installed)
    "CAM-ENTRY": {"ip": "192.168.1.104", "user": "admin", "password": "CHANGE_ME", "phase": 2, "gate": "entry"},
    "CAM-EXIT":  {"ip": "192.168.1.105", "user": "admin", "password": "CHANGE_ME", "phase": 2, "gate": "exit"},
}
```

---

## 3. How Camera Notifications Work

### 3.1 Core Concept — Pure Push, Zero Polling

Hikvision cameras run their own **ISAPI web server** on the local IP.  
You configure them **once** to push events to your backend via HTTP POST.  
After that, the camera calls your backend automatically every time an event fires.

```
ONE-TIME SETUP (per camera):
  Your Script → PUT /ISAPI/Event/notification/httpHosts → Camera
  (Registers backend IP as the HTTP destination)

RUNTIME (automatic forever):
  Camera detects event → HTTP POST → Your Backend
```

### 3.2 Event Flow — Phase 1 (Intrusion / Occupancy)

```
Camera edge AI detects vehicle in zone
         │
         ▼
Event trigger fires (fielddetection / regionEntrance / regionExiting)
         │
         ▼
Camera HTTP POSTs XML to http://192.168.1.50:8080/api/v1/events/camera
         │
         ▼
Backend parses XML → extracts eventType, regionID, detectionTarget
         │
     ┌───┴─────────────────────────┐
     ▼                             ▼
 Update occupancy state       Check violation rules
 (UC3)                         → fire alert if match (UC5/UC6)
```

### 3.3 Event Flow — Phase 2 (ANPR / Plate Recognition)

```
Vehicle approaches gate → ANPR camera reads license plate
         │
         ▼
Camera fires AccessControllerEvent with plate in cardNo field
         │
         ▼
Camera HTTP POSTs JSON to http://192.168.1.50:8080/api/v1/events/camera
         │
         ▼
Backend parses JSON → extracts plate, gate (entry/exit), timestamp
         │
     ┌───┴──────────────────────────────────┐
     ▼                                      ▼
 UC1: Increment entry/exit counter     UC4: Match plate to
 UC2: Log entry time, calc duration         employee/visitor DB
```

### 3.4 Event Types Reference

#### Phase 1 Events (XML format)

| eventType | Cameras | When Fires | Key Fields |
|-----------|---------|-----------|------------|
| `fielddetection` | All 3 | Vehicle/human in defined field zone | `regionID`, `detectionTarget` |
| `linedetection` | All 3 | Object crossed a virtual line | `regionID`, `detectionTarget` |
| `regionEntrance` | CAM-03 only | Object entered a region (deep learning) | `regionID` |
| `regionExiting` | CAM-03 only | Object exited a region (deep learning) | `regionID` |
| `VMD` | All 3 | Basic motion (fallback, low priority) | — |

#### Phase 2 Events (JSON format)

| eventType | Cameras | When Fires | Key Fields |
|-----------|---------|-----------|------------|
| `AccessControllerEvent` | ANPR cameras | Vehicle plate read at gate | `cardNo` (plate), `majorEventType`, `subEventType` |

### 3.5 Sample Payloads

#### Phase 1 — fielddetection (XML)
```xml
<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>DS-2CD3783G2-IZSU-001</deviceSerial>
  <channelID>1</channelID>
  <triggerTime>2026-02-20T08:00:00</triggerTime>
  <eventType>fielddetection</eventType>
  <eventDescription>Intrusion detected</eventDescription>
  <DetectionRegionList>
    <DetectionRegionEntry>
      <regionID>zone-A</regionID>
      <detectionTarget>vehicle</detectionTarget>
    </DetectionRegionEntry>
  </DetectionRegionList>
  <channelName>ParkingZone-A</channelName>
</EventNotificationAlert>
```

#### Phase 1 — regionEntrance (XML)
```xml
<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>DS-2CD3783G2-IZSU-001</deviceSerial>
  <channelID>1</channelID>
  <triggerTime>2026-02-20T08:05:00</triggerTime>
  <eventType>regionEntrance</eventType>
  <eventDescription>Vehicle entered region</eventDescription>
  <DetectionRegionList>
    <DetectionRegionEntry>
      <regionID>parking-row-A</regionID>
    </DetectionRegionEntry>
  </DetectionRegionList>
  <channelName>ParkingRow-A</channelName>
</EventNotificationAlert>
```

#### Phase 2 — AccessControllerEvent / ANPR (JSON)
```json
{
  "ipAddress": "192.168.1.104",
  "portNo": 80,
  "dateTime": "2026-02-20T09:00:00Z",
  "activePostCount": 1,
  "eventType": "AccessControllerEvent",
  "eventState": "active",
  "eventDescription": "Vehicle plate detected at entry gate",
  "deviceID": "CAM-ENTRY-001",
  "AccessControllerEvent": {
    "deviceName": "Entry Gate Camera",
    "majorEventType": 1,
    "subEventType": 1,
    "cardNo": "ABC-1234",
    "userType": "normal",
    "name": "Ahmed Al-Sayed",
    "employeeNoString": "EMP-042",
    "picEnable": true,
    "pictureURL": "http://192.168.1.104/ISAPI_FILES/snapshot.jpg"
  },
  "deviceSerial": "DS-2CD7A26-ENTRY-001"
}
```

---

## 4. Configuring Camera HTTP Push (Webhook)

### 4.1 Combined Setup Script (Phase 1 + Phase 2)

Run this script once when each phase of cameras is installed.  
It is safe to re-run — it will update existing config without duplicating.

```python
# scripts/setup/configure_cameras.py
"""
One-time camera configuration script — Phase 1 and Phase 2.
Registers backend as HTTP event target on all cameras.
Usage:
  Phase 1: python scripts/setup/configure_cameras.py --phase 1
  Phase 2: python scripts/setup/configure_cameras.py --phase 2
  All:     python scripts/setup/configure_cameras.py --phase all
"""

import argparse
import requests
from requests.auth import HTTPDigestAuth

BACKEND_IP = "192.168.1.50"
BACKEND_PORT = 8080
BACKEND_EVENT_PATH = "/api/v1/events/camera"

CAMERAS = {
    # Phase 1
    "CAM-01":    {"ip": "192.168.1.101", "user": "admin", "password": "CHANGE_ME", "phase": 1,
                  "events": ["fielddetection", "linedetection", "VMD"]},
    "CAM-02":    {"ip": "192.168.1.102", "user": "admin", "password": "CHANGE_ME", "phase": 1,
                  "events": ["fielddetection", "linedetection", "VMD"]},
    "CAM-03":    {"ip": "192.168.1.103", "user": "admin", "password": "CHANGE_ME", "phase": 1,
                  "events": ["fielddetection", "linedetection", "regionEntrance", "regionExiting", "VMD"]},
    # Phase 2 (ANPR)
    "CAM-ENTRY": {"ip": "192.168.1.104", "user": "admin", "password": "CHANGE_ME", "phase": 2,
                  "events": ["AccessControllerEvent"]},
    "CAM-EXIT":  {"ip": "192.168.1.105", "user": "admin", "password": "CHANGE_ME", "phase": 2,
                  "events": ["AccessControllerEvent"]},
}


def configure_http_host(camera_id: str, camera: dict) -> bool:
    """Register backend as the HTTP notification destination on camera."""
    url = f"http://{camera['ip']}/ISAPI/Event/notification/httpHosts"

    # Phase 2 ANPR cameras use JSON format; Phase 1 cameras use XML
    param_format = "JSON" if camera["phase"] == 2 else "XML"

    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<HttpHostNotificationList>
  <HttpHostNotification>
    <id>1</id>
    <url>{BACKEND_EVENT_PATH}</url>
    <protocolType>HTTP</protocolType>
    <parameterFormatType>{param_format}</parameterFormatType>
    <addressingFormatType>ipaddress</addressingFormatType>
    <ipAddress>{BACKEND_IP}</ipAddress>
    <portNo>{BACKEND_PORT}</portNo>
    <httpAuthenticationMethod>none</httpAuthenticationMethod>
  </HttpHostNotification>
</HttpHostNotificationList>"""

    try:
        resp = requests.put(
            url,
            data=payload,
            auth=HTTPDigestAuth(camera["user"], camera["password"]),
            headers={"Content-Type": "application/xml"},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"  ✅ HTTP host ({param_format}) configured on {camera_id}")
            return True
        else:
            print(f"  ❌ Failed on {camera_id}: HTTP {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"  ❌ Error on {camera_id}: {e}")
        return False


def configure_event_trigger(camera_id: str, camera: dict, event_type: str, channel: int = 1) -> bool:
    """Enable HTTP notification linkage on a specific event trigger."""
    url = f"http://{camera['ip']}/ISAPI/Event/triggers/{event_type}-{channel}"
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<EventTrigger>
  <id>{event_type}-{channel}</id>
  <eventType>{event_type}</eventType>
  <EventTriggerNotificationList>
    <EventTriggerNotification>
      <id>1</id>
      <notificationMethod>HTTP</notificationMethod>
      <httpHostID>1</httpHostID>
    </EventTriggerNotification>
  </EventTriggerNotificationList>
</EventTrigger>"""
    try:
        resp = requests.put(
            url,
            data=payload,
            auth=HTTPDigestAuth(camera["user"], camera["password"]),
            headers={"Content-Type": "application/xml"},
            timeout=10,
        )
        if resp.status_code == 200:
            print(f"    ✅ {event_type} trigger configured")
            return True
        elif resp.status_code == 400:
            print(f"    ⚠️  {event_type} not supported on {camera_id} (skipped)")
            return True
        else:
            print(f"    ❌ {event_type} failed: HTTP {resp.status_code}")
            return False
    except Exception as e:
        print(f"    ❌ Error: {e}")
        return False


def verify_connection(camera: dict) -> bool:
    try:
        resp = requests.get(
            f"http://{camera['ip']}/ISAPI/System/deviceInfo",
            auth=HTTPDigestAuth(camera["user"], camera["password"]),
            timeout=5,
        )
        return resp.status_code == 200
    except:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["1", "2", "all"], default="1")
    args = parser.parse_args()

    target_phases = {1, 2} if args.phase == "all" else {int(args.phase)}

    print(f"🔧 Damanat Camera Configuration — Phase {args.phase}")
    print("=" * 55)

    for camera_id, camera in CAMERAS.items():
        if camera["phase"] not in target_phases:
            continue

        print(f"\n📷 [{camera_id}] Phase {camera['phase']} — {camera['ip']}")

        if not verify_connection(camera):
            print(f"  ❌ Cannot reach {camera_id} — check IP/credentials")
            continue

        if not configure_http_host(camera_id, camera):
            continue

        for event in camera["events"]:
            configure_event_trigger(camera_id, camera, event)

    print(f"\n✅ Done. All configured cameras push to:")
    print(f"   http://{BACKEND_IP}:{BACKEND_PORT}{BACKEND_EVENT_PATH}")


if __name__ == "__main__":
    main()
```

### 4.2 ANPR Camera Additional Setup (Phase 2 Only)

For ANPR cameras, one additional step is needed to enable the plate recognition feature and link it to the event notification:

```python
# scripts/setup/configure_anpr_cameras.py
"""
ANPR-specific configuration for Phase 2 cameras.
Enables LPR (License Plate Recognition) and maps plates to vehicles.
Run after configure_cameras.py --phase 2
"""

import requests
from requests.auth import HTTPDigestAuth

ANPR_CAMERAS = {
    "CAM-ENTRY": {"ip": "192.168.1.104", "user": "admin", "password": "CHANGE_ME", "gate": "entry"},
    "CAM-EXIT":  {"ip": "192.168.1.105", "user": "admin", "password": "CHANGE_ME", "gate": "exit"},
}


def enable_anpr(camera_id: str, camera: dict) -> bool:
    """Enable ANPR/LPR on the camera via ISAPI."""
    url = f"http://{camera['ip']}/ISAPI/Traffic/channels/1/vehicleDetect"
    payload = """<?xml version="1.0" encoding="UTF-8"?>
<VehicleDetectCap>
  <enabled>true</enabled>
  <recognitionMode>plate</recognitionMode>
</VehicleDetectCap>"""
    try:
        resp = requests.put(
            url,
            data=payload,
            auth=HTTPDigestAuth(camera["user"], camera["password"]),
            headers={"Content-Type": "application/xml"},
            timeout=10,
        )
        print(f"  ✅ ANPR enabled on {camera_id}" if resp.status_code == 200
              else f"  ❌ Failed: {resp.status_code}")
        return resp.status_code == 200
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def register_vehicle_plates(camera_id: str, camera: dict, vehicles: list[dict]) -> bool:
    """
    Pre-register known vehicle plates on ANPR cameras.
    vehicles = [{"plate": "ABC-1234", "name": "Ahmed Al-Sayed", "type": "employee"}]
    In production: load from your vehicle DB.
    """
    for vehicle in vehicles:
        url = f"http://{camera['ip']}/ISAPI/AccessControl/UserInfo/SetUp?format=json"
        payload = {
            "UserInfo": {
                "employeeNo": vehicle.get("employee_id", vehicle["plate"]),
                "name": vehicle["name"],
                "userType": "normal" if vehicle["type"] == "employee" else "visitor",
                "Valid": {"enable": True},
                "cardNo": vehicle["plate"],  # plate number used as card number
            }
        }
        try:
            resp = requests.post(
                url,
                json=payload,
                auth=HTTPDigestAuth(camera["user"], camera["password"]),
                timeout=10,
            )
            if resp.status_code == 200:
                print(f"    ✅ Registered plate {vehicle['plate']} ({vehicle['name']})")
        except Exception as e:
            print(f"    ❌ Failed to register {vehicle['plate']}: {e}")
    return True


if __name__ == "__main__":
    print("🔧 ANPR Camera Setup — Phase 2")
    sample_vehicles = [
        {"plate": "ABC-1234", "name": "Ahmed Al-Sayed", "type": "employee", "employee_id": "EMP-001"},
        {"plate": "XYZ-5678", "name": "Visitor Pass 1", "type": "visitor", "employee_id": "VIS-001"},
    ]
    for camera_id, camera in ANPR_CAMERAS.items():
        print(f"\n📷 Configuring {camera_id} ({camera['gate']} gate)...")
        enable_anpr(camera_id, camera)
        register_vehicle_plates(camera_id, camera, sample_vehicles)
```

---

## 5. FastAPI Project Structure

### 5.1 Full Folder Structure (Phase 1 + Phase 2)

```
damanat-backend/
│
├── app/
│   ├── __init__.py
│   ├── main.py                        # FastAPI entry point, router registration
│   ├── config.py                      # All settings (camera IPs, DB URL, thresholds)
│   ├── database.py                    # DB connection + session factory
│   │
│   ├── models/                        # SQLAlchemy ORM models
│   │   ├── __init__.py
│   │   ├── camera_event.py            # Raw event log (all phases)
│   │   ├── zone_occupancy.py          # Zone occupancy state (UC3)
│   │   ├── alert.py                   # Alerts & violations (UC5, UC6)
│   │   ├── vehicle.py                 # 🔜 Phase 2: registered vehicles (employees/visitors)
│   │   └── entry_exit_log.py          # 🔜 Phase 2: entry/exit log per plate (UC1, UC2)
│   │
│   ├── schemas/                       # Pydantic schemas
│   │   ├── __init__.py
│   │   ├── camera_event.py
│   │   ├── zone_occupancy.py
│   │   ├── alert.py
│   │   ├── vehicle.py                 # 🔜 Phase 2
│   │   └── entry_exit_log.py          # 🔜 Phase 2
│   │
│   ├── routers/                       # One file per use case
│   │   ├── __init__.py
│   │   ├── events.py                  # POST /api/v1/events/camera  ← ALL events entry point
│   │   ├── occupancy.py               # GET  /api/v1/occupancy       (UC3)
│   │   ├── violations.py              # GET  /api/v1/violations       (UC5)
│   │   ├── intrusion.py               # GET  /api/v1/intrusions       (UC6)
│   │   ├── entry_exit.py              # 🔜 Phase 2: GET /api/v1/entry-exit  (UC1)
│   │   ├── parking_stats.py           # 🔜 Phase 2: GET /api/v1/stats       (UC2)
│   │   ├── vehicles.py                # 🔜 Phase 2: CRUD /api/v1/vehicles   (UC4)
│   │   └── health.py                  # GET  /api/v1/health
│   │
│   ├── services/
│   │   ├── __init__.py
│   │   ├── event_parser.py            # Parse raw XML/JSON → ParsedCameraEvent
│   │   ├── event_dispatcher.py        # Route event to correct handler
│   │   ├── occupancy_service.py       # UC3
│   │   ├── violation_service.py       # UC5
│   │   ├── intrusion_service.py       # UC6
│   │   ├── entry_exit_service.py      # 🔜 Phase 2: UC1 + UC2
│   │   ├── vehicle_service.py         # 🔜 Phase 2: UC4 (plate lookup)
│   │   └── alert_service.py           # Shared alert creator
│   │
│   └── utils/
│       ├── __init__.py
│       ├── xml_parser.py
│       ├── json_parser.py             # 🔜 Phase 2: parse ANPR JSON events
│       └── logger.py
│
├── scripts/
│   ├── setup/
│   │   ├── configure_cameras.py       # Phase 1 + 2: register HTTP push on all cameras
│   │   ├── configure_anpr_cameras.py  # 🔜 Phase 2: enable LPR + register plates
│   │   └── init_db.py                 # Create all DB tables (Phase 1 + 2 tables)
│   └── test/
│       ├── simulate_event.py          # Send fake events (Phase 1 + 2)
│       └── test_camera_conn.py
│
├── tests/
│   ├── test_event_parser.py
│   ├── test_occupancy_service.py
│   ├── test_violation_service.py
│   └── test_entry_exit_service.py     # 🔜 Phase 2
│
├── logs/
│   └── .gitkeep
│
├── .env
├── .env.example
├── requirements.txt
├── docker-compose.yml
└── README.md
```

> 🔜 = Phase 2 files. Create them now with empty stubs so the project structure is complete from day one. Fill implementation when ANPR cameras arrive.

### 5.2 `app/main.py` — Full (Phase 1 + 2 Routers)

```python
from fastapi import FastAPI
from app.routers import events, occupancy, violations, intrusion, health
# Phase 2 imports (uncomment when ANPR cameras are installed)
# from app.routers import entry_exit, parking_stats, vehicles
from app.database import create_tables

app = FastAPI(
    title="Damanat Parking Analytics API",
    description="AI Camera event processing for Damanat facility — Phase 1 + 2",
    version="2.0.0",
)

# Phase 1 routers
app.include_router(events.router,     prefix="/api/v1", tags=["Camera Events"])
app.include_router(occupancy.router,  prefix="/api/v1", tags=["Occupancy - UC3"])
app.include_router(violations.router, prefix="/api/v1", tags=["Violations - UC5"])
app.include_router(intrusion.router,  prefix="/api/v1", tags=["Intrusion - UC6"])
app.include_router(health.router,     prefix="/api/v1", tags=["Health"])

# Phase 2 routers (uncomment when ready)
# app.include_router(entry_exit.router,    prefix="/api/v1", tags=["Entry/Exit - UC1"])
# app.include_router(parking_stats.router, prefix="/api/v1", tags=["Parking Stats - UC2"])
# app.include_router(vehicles.router,      prefix="/api/v1", tags=["Vehicles - UC4"])

@app.on_event("startup")
async def startup():
    create_tables()  # Creates ALL tables (Phase 1 + 2) — safe to run at startup
```

### 5.3 `app/config.py`

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://damanat:damanat@localhost:5432/damanat_db"
    BACKEND_IP: str = "192.168.1.50"
    BACKEND_PORT: int = 8080

    # Phase 1 cameras
    CAMERAS: dict = {
        "CAM-01": {"ip": "192.168.1.101", "serial": "DS-2CD3681G2-001", "zone": "outdoor-perimeter", "phase": 1},
        "CAM-02": {"ip": "192.168.1.102", "serial": "DS-2CD3781G2-001", "zone": "indoor-parking",    "phase": 1},
        "CAM-03": {"ip": "192.168.1.103", "serial": "DS-2CD3783G2-001", "zone": "critical-zone",     "phase": 1},
        # Phase 2 — add when ANPR cameras arrive
        "CAM-ENTRY": {"ip": "192.168.1.104", "serial": "ANPR-ENTRY-001", "zone": "entry-gate", "phase": 2, "gate": "entry"},
        "CAM-EXIT":  {"ip": "192.168.1.105", "serial": "ANPR-EXIT-001",  "zone": "exit-gate",  "phase": 2, "gate": "exit"},
    }

    CAMERA_IP_MAP: dict = {
        "192.168.1.101": "CAM-01",
        "192.168.1.102": "CAM-02",
        "192.168.1.103": "CAM-03",
        "192.168.1.104": "CAM-ENTRY",
        "192.168.1.105": "CAM-EXIT",
    }

    OCCUPANCY_ALERT_THRESHOLD: float = 0.90
    INTRUSION_COOLDOWN_SECONDS: int = 30

    class Config:
        env_file = ".env"

settings = Settings()
```

### 5.4 `requirements.txt`

```
fastapi==0.110.0
uvicorn[standard]==0.29.0
sqlalchemy==2.0.28
psycopg2-binary==2.9.9
pydantic-settings==2.2.1
python-multipart==0.0.9
lxml==5.1.0
requests==2.31.0
httpx==0.27.0
python-dotenv==1.0.1
```

---

## 6. Database Schema

### Phase 1 Tables

```python
# app/models/camera_event.py
from sqlalchemy import Column, Integer, String, DateTime, Text
from app.database import Base

class CameraEvent(Base):
    __tablename__ = "camera_events"
    id               = Column(Integer, primary_key=True, index=True)
    camera_id        = Column(String(50), index=True)
    device_serial    = Column(String(100))
    channel_id       = Column(Integer)
    event_type       = Column(String(50), index=True)
    detection_target = Column(String(20))             # vehicle | human | others
    region_id        = Column(String(100))
    channel_name     = Column(String(100))
    trigger_time     = Column(DateTime, index=True)
    raw_payload      = Column(Text)
    created_at       = Column(DateTime)


# app/models/zone_occupancy.py
class ZoneOccupancy(Base):
    __tablename__ = "zone_occupancy"
    id             = Column(Integer, primary_key=True)
    zone_id        = Column(String(100), unique=True, index=True)
    camera_id      = Column(String(50))
    current_count  = Column(Integer, default=0)
    max_capacity   = Column(Integer, default=10)
    last_updated   = Column(DateTime)


# app/models/alert.py
class Alert(Base):
    __tablename__ = "alerts"
    id           = Column(Integer, primary_key=True, index=True)
    alert_type   = Column(String(50), index=True)     # intrusion | violation | occupancy_full
    camera_id    = Column(String(50))
    zone_id      = Column(String(100))
    event_type   = Column(String(50))
    description  = Column(Text)
    is_resolved  = Column(Integer, default=0)
    triggered_at = Column(DateTime, index=True)
    resolved_at  = Column(DateTime, nullable=True)
```

### Phase 2 Tables

```python
# app/models/vehicle.py  🔜 Phase 2
class Vehicle(Base):
    __tablename__ = "vehicles"
    id              = Column(Integer, primary_key=True)
    plate_number    = Column(String(50), unique=True, index=True)   # cardNo from ANPR event
    owner_name      = Column(String(200))
    vehicle_type    = Column(String(50))                             # employee | visitor | unknown
    employee_id     = Column(String(100), nullable=True)
    is_registered   = Column(Integer, default=1)                    # 0=unknown, 1=registered
    registered_at   = Column(DateTime)
    notes           = Column(Text, nullable=True)


# app/models/entry_exit_log.py  🔜 Phase 2
class EntryExitLog(Base):
    __tablename__ = "entry_exit_log"
    id             = Column(Integer, primary_key=True, index=True)
    plate_number   = Column(String(50), index=True)
    vehicle_id     = Column(Integer, nullable=True)     # FK to vehicles table (if known)
    vehicle_type   = Column(String(50))                 # employee | visitor | unknown
    gate           = Column(String(10), index=True)     # entry | exit
    camera_id      = Column(String(50))
    event_time     = Column(DateTime, index=True)
    # UC2 fields
    matched_entry_id = Column(Integer, nullable=True)   # FK to self (links exit to entry)
    parking_duration = Column(Integer, nullable=True)   # seconds parked (set on exit)
    created_at     = Column(DateTime)
```

---

## 7. Use Case Scripts — Phase 1

### 7.1 Camera Event Receiver (Single Entry Point — All Phases)

```python
# app/routers/events.py
"""
PRIMARY ENTRY POINT for ALL camera events — Phase 1 and Phase 2.
All cameras POST to: POST /api/v1/events/camera
Detects format (XML = Phase 1, JSON = Phase 2) and routes accordingly.
"""

from fastapi import APIRouter, Request, Depends
from sqlalchemy.orm import Session
from app.database import get_db
from app.services.event_parser import parse_camera_event
from app.services.event_dispatcher import dispatch_event
from app.utils.logger import get_logger
from datetime import datetime

router = APIRouter()
logger = get_logger(__name__)


@router.post("/events/camera", status_code=200)
async def receive_camera_event(request: Request, db: Session = Depends(get_db)):
    """
    Unified webhook — receives events from ALL cameras (Phase 1 XML + Phase 2 JSON).
    Always returns HTTP 200 to prevent camera retry storms.
    """
    try:
        raw_body = await request.body()
        if not raw_body:
            return {"status": "ignored", "reason": "empty body"}

        camera_ip = request.client.host
        content_type = request.headers.get("content-type", "")
        logger.info(f"Event from {camera_ip} | {len(raw_body)} bytes | {content_type}")

        # Parse into unified ParsedCameraEvent (handles XML and JSON)
        event = parse_camera_event(raw_body, camera_ip, content_type)
        logger.info(f"Parsed: type={event.event_type} target={event.detection_target} zone={event.region_id} plate={event.plate_number}")

        # Persist raw event
        from app.models.camera_event import CameraEvent
        db.add(CameraEvent(
            camera_id=event.camera_id,
            device_serial=event.device_serial,
            channel_id=event.channel_id,
            event_type=event.event_type,
            detection_target=event.detection_target,
            region_id=event.region_id,
            channel_name=event.channel_name,
            trigger_time=event.trigger_time,
            raw_payload=raw_body.decode("utf-8", errors="replace"),
            created_at=datetime.utcnow(),
        ))
        db.commit()

        # Dispatch to correct use-case handlers
        await dispatch_event(event, db)
        return {"status": "ok", "event_type": event.event_type}

    except Exception as e:
        logger.error(f"Event processing error: {e}", exc_info=True)
        return {"status": "error", "detail": str(e)}  # Still return 200


@router.get("/events")
def list_events(limit: int = 50, camera_id: str = None, event_type: str = None,
                db: Session = Depends(get_db)):
    from app.models.camera_event import CameraEvent
    q = db.query(CameraEvent)
    if camera_id:
        q = q.filter(CameraEvent.camera_id == camera_id)
    if event_type:
        q = q.filter(CameraEvent.event_type == event_type)
    return q.order_by(CameraEvent.created_at.desc()).limit(limit).all()
```

### 7.2 Event Parser (Phase 1 XML + Phase 2 JSON)

```python
# app/services/event_parser.py
"""
Parses both Phase 1 (XML) and Phase 2 (JSON/ANPR) camera event payloads.
Returns a unified ParsedCameraEvent regardless of source.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import xml.etree.ElementTree as ET
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)
NS = "http://www.isapi.org/ver20/XMLSchema"


@dataclass
class ParsedCameraEvent:
    camera_id: str
    device_serial: str
    channel_id: int
    event_type: str          # fielddetection | linedetection | regionEntrance | regionExiting | AccessControllerEvent
    detection_target: Optional[str]  # vehicle | human | others (Phase 1)
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: datetime
    raw_xml: str
    # Phase 2 ANPR fields
    plate_number: Optional[str] = None    # cardNo from AccessControllerEvent
    user_type: Optional[str] = None       # normal | visitor | blacklist
    person_name: Optional[str] = None
    employee_id: Optional[str] = None
    gate: Optional[str] = None            # entry | exit (from camera config)


def parse_camera_event(raw_body: bytes, camera_ip: str, content_type: str = "") -> ParsedCameraEvent:
    """Auto-detect format and parse accordingly."""
    is_json = "json" in content_type.lower() or raw_body.lstrip()[:1] == b"{"
    if is_json:
        return _parse_json_event(raw_body, camera_ip)
    else:
        return _parse_xml_event(raw_body, camera_ip)


def _parse_xml_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 1 XML events (fielddetection, regionEntrance, etc.)"""
    xml_str = raw_body.decode("utf-8", errors="replace")
    root = ET.fromstring(xml_str)

    def find(tag):
        el = root.find(f"{{{NS}}}{tag}") or root.find(tag)
        return el.text.strip() if el is not None and el.text else None

    def find_in(parent, tag):
        el = parent.find(f"{{{NS}}}{tag}") or parent.find(tag)
        return el.text.strip() if el is not None and el.text else None

    trigger_time = datetime.utcnow()
    t = find("triggerTime") or find("dateTime")
    if t:
        try:
            trigger_time = datetime.fromisoformat(t.replace("Z", "+00:00"))
        except:
            pass

    region_id, detection_target = None, None
    region_list = root.find(f"{{{NS}}}DetectionRegionList") or root.find("DetectionRegionList")
    if region_list is not None:
        entry = region_list.find(f"{{{NS}}}DetectionRegionEntry") or region_list.find("DetectionRegionEntry")
        if entry is not None:
            region_id = find_in(entry, "regionID")
            detection_target = find_in(entry, "detectionTarget")

    return ParsedCameraEvent(
        camera_id=settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}"),
        device_serial=find("deviceSerial") or "unknown",
        channel_id=int(find("channelID") or 1),
        event_type=find("eventType") or "unknown",
        detection_target=detection_target,
        region_id=region_id,
        channel_name=find("channelName"),
        trigger_time=trigger_time,
        raw_xml=xml_str,
    )


def _parse_json_event(raw_body: bytes, camera_ip: str) -> ParsedCameraEvent:
    """Parse Phase 2 JSON events (AccessControllerEvent from ANPR cameras)."""
    data = json.loads(raw_body.decode("utf-8", errors="replace"))

    trigger_time = datetime.utcnow()
    dt = data.get("dateTime", "")
    if dt:
        try:
            trigger_time = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except:
            pass

    acs = data.get("AccessControllerEvent", {})
    camera_id = settings.CAMERA_IP_MAP.get(camera_ip, f"UNKNOWN-{camera_ip}")

    # Determine gate direction from camera config
    cam_config = settings.CAMERAS.get(camera_id, {})
    gate = cam_config.get("gate")  # "entry" or "exit"

    return ParsedCameraEvent(
        camera_id=camera_id,
        device_serial=data.get("deviceSerial", data.get("deviceID", "unknown")),
        channel_id=data.get("channelID", 1),
        event_type=data.get("eventType", "unknown"),
        detection_target="vehicle",   # ANPR events are always vehicle
        region_id=gate,               # gate = entry | exit
        channel_name=acs.get("deviceName"),
        trigger_time=trigger_time,
        raw_xml=raw_body.decode("utf-8", errors="replace"),
        # ANPR-specific
        plate_number=acs.get("cardNo"),
        user_type=acs.get("userType"),
        person_name=acs.get("name"),
        employee_id=acs.get("employeeNoString"),
        gate=gate,
    )
```

### 7.3 Event Dispatcher (Phase 1 + Phase 2)

```python
# app/services/event_dispatcher.py
"""Routes events to correct use-case handlers — Phase 1 and Phase 2."""

from app.services.event_parser import ParsedCameraEvent
from app.services.occupancy_service import handle_occupancy_event
from app.services.violation_service import handle_violation_event
from app.services.intrusion_service import handle_intrusion_event
from app.utils.logger import get_logger
from sqlalchemy.orm import Session

logger = get_logger(__name__)


async def dispatch_event(event: ParsedCameraEvent, db: Session):
    is_vehicle = event.detection_target in ("vehicle", None)

    # ── PHASE 1 ───────────────────────────────────────────────────────────
    # UC3: Occupancy — region entrance/exit (CAM-03 only)
    if event.event_type in ("regionEntrance", "regionExiting"):
        await handle_occupancy_event(event, db)

    # UC5: Violation alerts
    if event.event_type in ("fielddetection", "linedetection", "regionEntrance") and is_vehicle:
        await handle_violation_event(event, db)

    # UC6: Intrusion detection
    if event.event_type in ("fielddetection", "regionEntrance") and is_vehicle:
        await handle_intrusion_event(event, db)

    # ── PHASE 2 ───────────────────────────────────────────────────────────
    # UC1 + UC2 + UC4: ANPR gate events
    if event.event_type == "AccessControllerEvent" and event.plate_number:
        try:
            from app.services.entry_exit_service import handle_anpr_event
            await handle_anpr_event(event, db)
        except ImportError:
            logger.warning("entry_exit_service not yet implemented (Phase 2 pending)")
```

### 7.4 UC3 — Occupancy Service

```python
# app/services/occupancy_service.py
"""
UC3: Parking Occupancy Monitoring
Camera: CAM-03 (DS-2CD3783G2 AcuSense) only
Events: regionEntrance (+1), regionExiting (-1)
Camera config: Draw parking row zones on CAM-03 web UI with regionID labels
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.zone_occupancy import ZoneOccupancy
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def handle_occupancy_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or f"{event.camera_id}-default"
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        zone = ZoneOccupancy(zone_id=zone_id, camera_id=event.camera_id,
                             current_count=0, max_capacity=10, last_updated=datetime.utcnow())
        db.add(zone)

    if event.event_type == "regionEntrance":
        zone.current_count = max(0, zone.current_count + 1)
    elif event.event_type == "regionExiting":
        zone.current_count = max(0, zone.current_count - 1)

    zone.last_updated = datetime.utcnow()
    db.commit()
    logger.info(f"[UC3] {zone_id}: {zone.current_count}/{zone.max_capacity}")

    if zone.max_capacity and (zone.current_count / zone.max_capacity) >= settings.OCCUPANCY_ALERT_THRESHOLD:
        await create_alert(db, "occupancy_full", event.camera_id, zone_id, event.event_type,
                           f"Zone {zone_id} at {int(zone.current_count/zone.max_capacity*100)}% capacity")
```

### 7.5 UC5 — Violation Service

```python
# app/services/violation_service.py
"""
UC5: Proactive Violation Alerts
Events: fielddetection (restricted zone), linedetection (forbidden line), regionEntrance
Config: Add zone IDs matching camera-configured zone names to RESTRICTED_ZONES
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

RESTRICTED_ZONES = {"restricted-vip", "no-parking-zone", "emergency-exit", "loading-bay"}
ALWAYS_VIOLATION_EVENTS = {"linedetection"}


async def handle_violation_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or "unknown-zone"
    if zone_id not in RESTRICTED_ZONES and event.event_type not in ALWAYS_VIOLATION_EVENTS:
        return

    cooldown = timedelta(seconds=settings.INTRUSION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id, Alert.alert_type == "violation",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    if recent:
        return

    desc = (f"Line crossing in zone {zone_id}" if event.event_type == "linedetection"
            else f"Vehicle in restricted zone: {zone_id}")
    logger.warning(f"[UC5] VIOLATION: {desc}")
    await create_alert(db, "violation", event.camera_id, zone_id, event.event_type, desc)
```

### 7.6 UC6 — Intrusion Service

```python
# app/services/intrusion_service.py
"""
UC6: Intrusion Detection
Events: fielddetection, regionEntrance — vehicle only
Note: Cannot verify plate identity without ANPR (Phase 2).
      Authorization check by plate is added in Phase 2 via entry_exit_service.
"""

from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.config import settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

MONITORED_INTRUSION_ZONES = {"emergency-exit", "staff-only-area", "after-hours-zone"}


async def handle_intrusion_event(event: ParsedCameraEvent, db: Session):
    zone_id = event.region_id or f"{event.camera_id}-field"
    if zone_id not in MONITORED_INTRUSION_ZONES and event.region_id is not None:
        return

    cooldown = timedelta(seconds=settings.INTRUSION_COOLDOWN_SECONDS)
    recent = db.query(Alert).filter(
        Alert.zone_id == zone_id, Alert.alert_type == "intrusion",
        Alert.triggered_at >= datetime.utcnow() - cooldown
    ).first()
    if recent:
        return

    desc = f"Vehicle intrusion in {zone_id} — {event.camera_id}"
    logger.warning(f"[UC6] INTRUSION: {desc}")
    await create_alert(db, "intrusion", event.camera_id, zone_id, event.event_type, desc)
```

### 7.7 Alert Service (Shared)

```python
# app/services/alert_service.py
from datetime import datetime
from sqlalchemy.orm import Session
from app.models.alert import Alert
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def create_alert(db, alert_type, camera_id, zone_id, event_type, description):
    db.add(Alert(alert_type=alert_type, camera_id=camera_id, zone_id=zone_id,
                 event_type=event_type, description=description,
                 is_resolved=0, triggered_at=datetime.utcnow()))
    db.commit()
    logger.warning(f"[ALERT][{alert_type.upper()}] {description}")
    # Extend here: push notification, SMS, email, etc.
```

---

## 8. Use Case Scripts — Phase 2 (ANPR)

> These files should be **created as stubs on Day 1** and filled when ANPR cameras are installed.  
> The dispatcher already calls `handle_anpr_event` — no dispatcher changes needed in Phase 2.

### 8.1 UC1 + UC2 + UC4 — ANPR Entry/Exit Service

```python
# app/services/entry_exit_service.py
"""
Phase 2: UC1 (Entry/Exit Counting) + UC2 (Parking Time) + UC4 (Vehicle ID)
Camera: CAM-ENTRY (192.168.1.104) + CAM-EXIT (192.168.1.105)
Event: AccessControllerEvent (JSON)
Key field: event.plate_number (= cardNo from camera)

How it works:
  - CAM-ENTRY fires AccessControllerEvent → handle_anpr_event logs ENTRY + resolves vehicle
  - CAM-EXIT fires AccessControllerEvent  → handle_anpr_event logs EXIT + calculates duration
  - Both are stored in entry_exit_log table
  - Daily counts + avg duration derived via SQL queries in parking_stats router
"""

from datetime import datetime
from sqlalchemy.orm import Session
from app.models.entry_exit_log import EntryExitLog
from app.models.vehicle import Vehicle
from app.services.event_parser import ParsedCameraEvent
from app.services.alert_service import create_alert
from app.utils.logger import get_logger

logger = get_logger(__name__)


async def handle_anpr_event(event: ParsedCameraEvent, db: Session):
    plate = event.plate_number
    gate = event.gate  # "entry" or "exit"

    if not plate:
        logger.warning(f"[Phase2] ANPR event with no plate from {event.camera_id} — skipped")
        return

    # UC4: Resolve vehicle identity from DB
    vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    vehicle_type = vehicle.vehicle_type if vehicle else "unknown"
    person_name = vehicle.owner_name if vehicle else event.person_name or "Unknown"

    logger.info(f"[UC1] Gate={gate} | Plate={plate} | Type={vehicle_type} | Name={person_name}")

    # Create entry/exit log record
    log_entry = EntryExitLog(
        plate_number=plate,
        vehicle_id=vehicle.id if vehicle else None,
        vehicle_type=vehicle_type,
        gate=gate,
        camera_id=event.camera_id,
        event_time=event.trigger_time,
        created_at=datetime.utcnow(),
    )

    # UC2: If this is an EXIT, find the matching ENTRY and calculate duration
    if gate == "exit":
        matching_entry = (
            db.query(EntryExitLog)
            .filter(
                EntryExitLog.plate_number == plate,
                EntryExitLog.gate == "entry",
                EntryExitLog.matched_entry_id == None,  # not yet matched
            )
            .order_by(EntryExitLog.event_time.desc())
            .first()
        )
        if matching_entry:
            duration_seconds = int((event.trigger_time - matching_entry.event_time).total_seconds())
            log_entry.matched_entry_id = matching_entry.id
            log_entry.parking_duration = duration_seconds
            matching_entry.matched_entry_id = log_entry.id  # cross-reference
            logger.info(f"[UC2] Plate={plate} parked for {duration_seconds // 60} min")
        else:
            logger.warning(f"[UC2] No matching entry found for plate {plate} at exit")

    # UC4: Alert if unknown vehicle
    if not vehicle:
        await create_alert(
            db=db,
            alert_type="unknown_vehicle",
            camera_id=event.camera_id,
            zone_id=gate,
            event_type=event.event_type,
            description=f"Unregistered vehicle at {gate} gate: plate {plate}",
        )

    db.add(log_entry)
    db.commit()
```

### 8.2 UC1 — Entry/Exit Router

```python
# app/routers/entry_exit.py
"""UC1: Entry/Exit Counting endpoints"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models.entry_exit_log import EntryExitLog
from datetime import datetime, date

router = APIRouter()


@router.get("/entry-exit", summary="UC1 — Get entry/exit log")
def get_entry_exit_log(limit: int = 50, gate: str = None, db: Session = Depends(get_db)):
    """Returns chronological entry/exit events with plate, type, and time."""
    q = db.query(EntryExitLog)
    if gate:
        q = q.filter(EntryExitLog.gate == gate)
    return q.order_by(EntryExitLog.event_time.desc()).limit(limit).all()


@router.get("/entry-exit/count/today", summary="UC1 — Today's vehicle count")
def get_today_counts(db: Session = Depends(get_db)):
    """Returns total entries and exits for today."""
    today = date.today()
    entries = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "entry",
        func.date(EntryExitLog.event_time) == today,
    ).scalar()
    exits = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "exit",
        func.date(EntryExitLog.event_time) == today,
    ).scalar()
    return {"date": str(today), "entries": entries, "exits": exits,
            "currently_parked": max(0, entries - exits)}
```

### 8.3 UC2 — Parking Stats Router

```python
# app/routers/parking_stats.py
"""UC2: Average Parking Time & Daily Vehicle Count"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.database import get_db
from app.models.entry_exit_log import EntryExitLog
from datetime import date

router = APIRouter()


@router.get("/stats/parking-time", summary="UC2 — Average parking duration")
def get_avg_parking_time(target_date: str = None, db: Session = Depends(get_db)):
    """
    Returns average parking duration in minutes for a given date.
    Only includes vehicles with matched entry+exit pairs.
    """
    q = db.query(func.avg(EntryExitLog.parking_duration)).filter(
        EntryExitLog.gate == "exit",
        EntryExitLog.parking_duration != None,
    )
    if target_date:
        q = q.filter(func.date(EntryExitLog.event_time) == target_date)

    avg_seconds = q.scalar() or 0
    return {
        "date": target_date or str(date.today()),
        "avg_parking_minutes": round(avg_seconds / 60, 1),
        "avg_parking_seconds": round(avg_seconds, 0),
    }


@router.get("/stats/daily", summary="UC2 — Daily vehicle count summary")
def get_daily_stats(target_date: str = None, db: Session = Depends(get_db)):
    """Returns total vehicles in/out and average parking time per day."""
    target = target_date or str(date.today())
    total = db.query(func.count(EntryExitLog.id)).filter(
        EntryExitLog.gate == "entry",
        func.date(EntryExitLog.event_time) == target,
    ).scalar()
    avg_dur = db.query(func.avg(EntryExitLog.parking_duration)).filter(
        EntryExitLog.gate == "exit",
        EntryExitLog.parking_duration != None,
        func.date(EntryExitLog.event_time) == target,
    ).scalar() or 0
    return {"date": target, "total_vehicles": total,
            "avg_parking_minutes": round(avg_dur / 60, 1)}
```

### 8.4 UC4 — Vehicle Management Router

```python
# app/routers/vehicles.py
"""UC4: Vehicle Identity & Classification — CRUD for registered vehicles"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models.vehicle import Vehicle
from datetime import datetime

router = APIRouter()


@router.get("/vehicles", summary="UC4 — List registered vehicles")
def list_vehicles(vehicle_type: str = None, db: Session = Depends(get_db)):
    q = db.query(Vehicle)
    if vehicle_type:
        q = q.filter(Vehicle.vehicle_type == vehicle_type)
    return q.all()


@router.post("/vehicles", summary="UC4 — Register a new vehicle")
def register_vehicle(plate: str, owner_name: str, vehicle_type: str,
                     employee_id: str = None, db: Session = Depends(get_db)):
    """Register an employee or visitor vehicle by plate number."""
    existing = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Plate {plate} already registered")
    vehicle = Vehicle(plate_number=plate, owner_name=owner_name,
                      vehicle_type=vehicle_type, employee_id=employee_id,
                      is_registered=1, registered_at=datetime.utcnow())
    db.add(vehicle)
    db.commit()
    return {"status": "registered", "plate": plate}


@router.delete("/vehicles/{plate}", summary="UC4 — Remove a vehicle")
def remove_vehicle(plate: str, db: Session = Depends(get_db)):
    vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    db.delete(vehicle)
    db.commit()
    return {"status": "removed", "plate": plate}


@router.get("/vehicles/lookup/{plate}", summary="UC4 — Look up a plate number")
def lookup_vehicle(plate: str, db: Session = Depends(get_db)):
    vehicle = db.query(Vehicle).filter(Vehicle.plate_number == plate).first()
    if not vehicle:
        return {"plate": plate, "status": "unknown", "registered": False}
    return {"plate": plate, "status": "known", "registered": True,
            "owner": vehicle.owner_name, "type": vehicle.vehicle_type}
```

### 8.5 Event Simulator — Phase 1 + Phase 2

```python
# scripts/test/simulate_event.py
"""Send test events to backend. Supports Phase 1 (XML) and Phase 2 (JSON/ANPR)."""

import argparse
import requests
from datetime import datetime

BACKEND_URL = "http://192.168.1.50:8080/api/v1/events/camera"

XML_TEMPLATES = {
    "fielddetection": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>fielddetection</eventType>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID><detectionTarget>{target}</detectionTarget>
  </DetectionRegionEntry></DetectionRegionList>
  <channelName>TestChannel</channelName>
</EventNotificationAlert>""",

    "regionEntrance": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>regionEntrance</eventType>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",

    "regionExiting": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>regionExiting</eventType>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",

    "linedetection": """<?xml version="1.0" encoding="utf-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.isapi.org/ver20/XMLSchema">
  <deviceSerial>TEST-SIM</deviceSerial><channelID>1</channelID>
  <triggerTime>{time}</triggerTime><eventType>linedetection</eventType>
  <eventDescription>Test</eventDescription>
  <DetectionRegionList><DetectionRegionEntry>
    <regionID>{zone}</regionID><detectionTarget>{target}</detectionTarget>
  </DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>""",
}

JSON_ANPR_TEMPLATE = {
    "eventType": "AccessControllerEvent",
    "eventState": "active",
    "eventDescription": "Test ANPR event",
    "dateTime": "",
    "deviceSerial": "TEST-ANPR-SIM",
    "AccessControllerEvent": {
        "deviceName": "Test ANPR Camera",
        "majorEventType": 1,
        "subEventType": 1,
        "cardNo": "",       # plate number
        "userType": "normal",
        "name": "Test Vehicle",
        "employeeNoString": "EMP-TEST",
    }
}


def simulate_xml(event_type, zone, target, source_ip):
    body = XML_TEMPLATES[event_type].format(
        time=datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        zone=zone, target=target,
    )
    resp = requests.post(BACKEND_URL, data=body.encode(),
                         headers={"Content-Type": "application/xml",
                                  "X-Forwarded-For": source_ip}, timeout=10)
    print(f"✅ {event_type} (XML) → HTTP {resp.status_code}: {resp.json()}")


def simulate_anpr(plate, gate_ip):
    payload = JSON_ANPR_TEMPLATE.copy()
    payload["dateTime"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    payload["AccessControllerEvent"] = {**payload["AccessControllerEvent"], "cardNo": plate}
    resp = requests.post(BACKEND_URL, json=payload,
                         headers={"Content-Type": "application/json",
                                  "X-Forwarded-For": gate_ip}, timeout=10)
    print(f"✅ ANPR (JSON) plate={plate} gate={gate_ip} → HTTP {resp.status_code}: {resp.json()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="fielddetection",
                        choices=list(XML_TEMPLATES.keys()) + ["anpr"])
    parser.add_argument("--zone", default="zone-A")
    parser.add_argument("--target", default="vehicle")
    parser.add_argument("--ip", default="192.168.1.103")
    parser.add_argument("--plate", default="ABC-1234")
    args = parser.parse_args()

    if args.event == "anpr":
        simulate_anpr(args.plate, args.ip)
    else:
        simulate_xml(args.event, args.zone, args.target, args.ip)
```

---

## 9. Team Guidelines

### 9.1 Where to Put New Files

| New thing to add | Where to put it |
|-----------------|-----------------|
| New camera | Add to `config.py` CAMERAS + CAMERA_IP_MAP |
| New event type handler | Add service in `services/`, register in `event_dispatcher.py` |
| New API endpoint | Add router in `routers/`, register in `main.py` |
| New DB table | Add model in `models/`, it auto-creates on startup |
| New test script | Add to `scripts/test/` |
| New setup script | Add to `scripts/setup/` |

### 9.2 Code Standards
- Always use `get_logger(__name__)` — no `print()` in production code
- Never hardcode IPs — use `settings` from `config.py`
- Always add a docstring to every service file explaining: Purpose, Camera, Event type, ISAPI endpoint
- Always return HTTP 200 from the camera webhook — never let exceptions bubble up to the camera
- Phase 2 stubs: create the file, add the docstring, raise `NotImplementedError` — don't leave empty files

### 9.3 Environment Setup

```bash
git clone <repo-url> && cd damanat-backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env         # then edit .env with real IPs and DB URL
python scripts/setup/init_db.py                     # create DB tables
python scripts/setup/configure_cameras.py --phase 1 # configure Phase 1 cameras
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
# Open: http://192.168.1.50:8080/docs
```

### 9.4 Phase 2 Activation Checklist

When ANPR cameras are physically installed:

- [ ] Update IP addresses in `.env` for `CAM-ENTRY` and `CAM-EXIT`
- [ ] Run `python scripts/setup/configure_cameras.py --phase 2`
- [ ] Run `python scripts/setup/configure_anpr_cameras.py`
- [ ] Fill in `entry_exit_service.py` implementation (stub → full code)
- [ ] Uncomment Phase 2 router imports in `app/main.py`
- [ ] Import vehicle data (employees + visitors) via `POST /api/v1/vehicles`
- [ ] Test with: `python scripts/test/simulate_event.py --event anpr --plate ABC-1234 --ip 192.168.1.104`
- [ ] Verify `GET /api/v1/entry-exit/count/today` returns correct counts

---

## 10. Testing & Validation

### Quick Test Commands

```bash
# Phase 1 — Occupancy (UC3)
python scripts/test/simulate_event.py --event regionEntrance --zone parking-row-A --ip 192.168.1.103
python scripts/test/simulate_event.py --event regionEntrance --zone parking-row-A --ip 192.168.1.103
python scripts/test/simulate_event.py --event regionExiting  --zone parking-row-A --ip 192.168.1.103
curl http://192.168.1.50:8080/api/v1/occupancy

# Phase 1 — Violation (UC5)
python scripts/test/simulate_event.py --event fielddetection --zone restricted-vip --target vehicle --ip 192.168.1.101
curl http://192.168.1.50:8080/api/v1/violations

# Phase 1 — Intrusion (UC6)
python scripts/test/simulate_event.py --event fielddetection --zone emergency-exit --target vehicle --ip 192.168.1.102
curl http://192.168.1.50:8080/api/v1/intrusions

# Phase 2 — ANPR Entry (UC1 + UC4)
python scripts/test/simulate_event.py --event anpr --plate ABC-1234 --ip 192.168.1.104
curl http://192.168.1.50:8080/api/v1/entry-exit/count/today

# Phase 2 — ANPR Exit (UC2)
python scripts/test/simulate_event.py --event anpr --plate ABC-1234 --ip 192.168.1.105
curl "http://192.168.1.50:8080/api/v1/stats/parking-time"
```

### All API Endpoints

| Phase | Method | Endpoint | Description |
|-------|--------|----------|-------------|
| Both | `POST` | `/api/v1/events/camera` | Camera webhook (all events) |
| Both | `GET` | `/api/v1/events` | Raw event log |
| 1 | `GET` | `/api/v1/occupancy` | Zone occupancy (UC3) |
| 1 | `GET` | `/api/v1/violations` | Violation alerts (UC5) |
| 1 | `PUT` | `/api/v1/violations/{id}/resolve` | Resolve violation |
| 1 | `GET` | `/api/v1/intrusions` | Intrusion alerts (UC6) |
| 2 | `GET` | `/api/v1/entry-exit` | Entry/exit log (UC1) |
| 2 | `GET` | `/api/v1/entry-exit/count/today` | Today's vehicle count (UC1) |
| 2 | `GET` | `/api/v1/stats/parking-time` | Avg parking duration (UC2) |
| 2 | `GET` | `/api/v1/stats/daily` | Daily summary (UC2) |
| 2 | `GET` | `/api/v1/vehicles` | Registered vehicles (UC4) |
| 2 | `POST` | `/api/v1/vehicles` | Register vehicle (UC4) |
| 2 | `GET` | `/api/v1/vehicles/lookup/{plate}` | Look up plate (UC4) |
| Both | `GET` | `/api/v1/health` | System health |

---

## 11. Complete Boilerplate Files (Production Ready)

> These are the infrastructure files that complete the project. Every file referenced in sections above is fully implemented here.

---

### 11.1 `app/database.py`

```python
# app/database.py
"""
Database connection, session management, and table creation.
Uses SQLAlchemy with PostgreSQL. All models are auto-imported here
so create_tables() creates every table in one call.
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings

engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,          # Auto-reconnect if DB connection drops
    pool_size=10,
    max_overflow=20,
    echo=False,                  # Set True to log all SQL queries (debug only)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    """FastAPI dependency — yields a DB session and closes it after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_tables():
    """
    Creates all DB tables on startup. Safe to call multiple times.
    Import all models here so SQLAlchemy knows about them.
    """
    # Phase 1 models
    from app.models.camera_event import CameraEvent       # noqa
    from app.models.zone_occupancy import ZoneOccupancy   # noqa
    from app.models.alert import Alert                     # noqa
    # Phase 2 models
    from app.models.vehicle import Vehicle                 # noqa
    from app.models.entry_exit_log import EntryExitLog     # noqa

    Base.metadata.create_all(bind=engine)
```

---

### 11.2 `app/utils/logger.py`

```python
# app/utils/logger.py
"""
Centralised logging configuration for the entire application.
Logs to console and to a rotating file in /logs/.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_configured = False


def _configure_root_logger():
    global _configured
    if _configured:
        return
    _configured = True

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(LOG_LEVEL)
    console.setFormatter(fmt)

    # Rotating file handler — keeps last 10 × 5MB log files
    file_handler = RotatingFileHandler(
        filename=os.path.join(LOG_DIR, "events.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(LOG_LEVEL)
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.addHandler(console)
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger. Call this at the top of every module."""
    _configure_root_logger()
    return logging.getLogger(name)
```

---

### 11.3 `app/utils/xml_parser.py`

```python
# app/utils/xml_parser.py
"""
Helpers for parsing Hikvision ISAPI XML event payloads.
All Phase 1 cameras send EventNotificationAlert v2.0 XML.
"""

import xml.etree.ElementTree as ET
from typing import Optional

NS = "http://www.isapi.org/ver20/XMLSchema"


def find_text(root: ET.Element, tag: str) -> Optional[str]:
    """Find a tag with or without the ISAPI namespace and return its text."""
    el = root.find(f"{{{NS}}}{tag}")
    if el is None:
        el = root.find(tag)
    return el.text.strip() if el is not None and el.text else None


def find_text_in(parent: ET.Element, tag: str) -> Optional[str]:
    """Same as find_text but searches within a given parent element."""
    el = parent.find(f"{{{NS}}}{tag}")
    if el is None:
        el = parent.find(tag)
    return el.text.strip() if el is not None and el.text else None


def parse_detection_region(root: ET.Element) -> tuple[Optional[str], Optional[str]]:
    """
    Extract (region_id, detection_target) from DetectionRegionList.
    Returns the first region entry found.
    """
    region_list = (
        root.find(f"{{{NS}}}DetectionRegionList") or
        root.find("DetectionRegionList")
    )
    if region_list is None:
        return None, None

    entry = (
        region_list.find(f"{{{NS}}}DetectionRegionEntry") or
        region_list.find("DetectionRegionEntry")
    )
    if entry is None:
        return None, None

    region_id = find_text_in(entry, "regionID")
    detection_target = find_text_in(entry, "detectionTarget")
    return region_id, detection_target


def safe_parse_xml(raw_body: bytes) -> Optional[ET.Element]:
    """Parse XML bytes safely. Returns None on parse error."""
    try:
        return ET.fromstring(raw_body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return None
```

---

### 11.4 `app/utils/json_parser.py`

```python
# app/utils/json_parser.py
"""
Helpers for parsing Phase 2 ANPR JSON event payloads.
ANPR cameras send AccessControllerEvent as JSON.
"""

import json
from typing import Optional, Any


def safe_parse_json(raw_body: bytes) -> Optional[dict]:
    """Parse JSON bytes safely. Returns None on error."""
    try:
        return json.loads(raw_body.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def get_nested(data: dict, *keys: str, default: Any = None) -> Any:
    """Safely navigate nested dict keys. Returns default if any key is missing."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
        if current is default:
            return default
    return current


def is_json_body(raw_body: bytes, content_type: str = "") -> bool:
    """Detect if the raw body is JSON (by content-type or by inspecting first byte)."""
    if "json" in content_type.lower():
        return True
    stripped = raw_body.lstrip()
    return stripped.startswith(b"{") or stripped.startswith(b"[")
```

---

### 11.5 All Pydantic Schemas

```python
# app/schemas/camera_event.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class CameraEventOut(BaseModel):
    id: int
    camera_id: str
    device_serial: str
    channel_id: Optional[int]
    event_type: str
    detection_target: Optional[str]
    region_id: Optional[str]
    channel_name: Optional[str]
    trigger_time: Optional[datetime]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True
```

```python
# app/schemas/zone_occupancy.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class ZoneOccupancyOut(BaseModel):
    id: int
    zone_id: str
    camera_id: str
    current_count: int
    max_capacity: int
    occupancy_percent: Optional[float] = None
    is_full: Optional[bool] = None
    last_updated: Optional[datetime]

    class Config:
        from_attributes = True


class ZoneCapacityUpdate(BaseModel):
    max_capacity: int
```

```python
# app/schemas/alert.py
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class AlertOut(BaseModel):
    id: int
    alert_type: str
    camera_id: str
    zone_id: Optional[str]
    event_type: Optional[str]
    description: Optional[str]
    is_resolved: int
    triggered_at: datetime
    resolved_at: Optional[datetime]

    class Config:
        from_attributes = True
```

```python
# app/schemas/vehicle.py  🔜 Phase 2
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class VehicleCreate(BaseModel):
    plate_number: str
    owner_name: str
    vehicle_type: str        # employee | visitor
    employee_id: Optional[str] = None
    notes: Optional[str] = None


class VehicleOut(BaseModel):
    id: int
    plate_number: str
    owner_name: str
    vehicle_type: str
    employee_id: Optional[str]
    is_registered: int
    registered_at: Optional[datetime]
    notes: Optional[str]

    class Config:
        from_attributes = True
```

```python
# app/schemas/entry_exit_log.py  🔜 Phase 2
from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class EntryExitLogOut(BaseModel):
    id: int
    plate_number: str
    vehicle_type: Optional[str]
    gate: str
    camera_id: str
    event_time: datetime
    parking_duration: Optional[int]   # seconds
    created_at: Optional[datetime]

    class Config:
        from_attributes = True


class DailyStatsOut(BaseModel):
    date: str
    total_vehicles: int
    avg_parking_minutes: float
```

---

### 11.6 `app/routers/health.py`

```python
# app/routers/health.py
"""
System health check endpoint.
Returns status of backend + DB + camera reachability.
"""

import requests
from requests.auth import HTTPDigestAuth
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.database import get_db
from app.config import settings
from datetime import datetime

router = APIRouter()


@router.get("/health", summary="System health check")
def health_check(db: Session = Depends(get_db)):
    """
    Returns:
    - Backend status
    - Database connectivity
    - Camera reachability (ping ISAPI on each camera)
    """
    result = {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "backend": "ok",
        "database": "unknown",
        "cameras": {},
    }

    # Check database
    try:
        db.execute(text("SELECT 1"))
        result["database"] = "ok"
    except Exception as e:
        result["database"] = f"error: {str(e)}"
        result["status"] = "degraded"

    # Ping each camera
    for cam_id, cam in settings.CAMERAS.items():
        try:
            resp = requests.get(
                f"http://{cam['ip']}/ISAPI/System/deviceInfo",
                auth=HTTPDigestAuth(cam["user"], cam["password"]),
                timeout=3,
            )
            result["cameras"][cam_id] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
        except requests.exceptions.ConnectionError:
            result["cameras"][cam_id] = "unreachable"
            result["status"] = "degraded"
        except Exception as e:
            result["cameras"][cam_id] = f"error: {str(e)}"

    return result
```

---

### 11.7 `app/routers/occupancy.py` (Updated — with capacity management)

```python
# app/routers/occupancy.py
"""UC3: Parking Occupancy — read + capacity management endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from app.database import get_db
from app.models.zone_occupancy import ZoneOccupancy
from app.schemas.zone_occupancy import ZoneOccupancyOut, ZoneCapacityUpdate

router = APIRouter()


@router.get("/occupancy", response_model=list[ZoneOccupancyOut])
def get_all_occupancy(db: Session = Depends(get_db)):
    """Current vehicle count for all zones."""
    zones = db.query(ZoneOccupancy).all()
    for z in zones:
        z.occupancy_percent = round((z.current_count / z.max_capacity) * 100, 1) if z.max_capacity else 0
        z.is_full = z.current_count >= z.max_capacity
    return zones


@router.get("/occupancy/{zone_id}", response_model=ZoneOccupancyOut)
def get_zone_occupancy(zone_id: str, db: Session = Depends(get_db)):
    """Occupancy for a specific zone."""
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' not found")
    zone.occupancy_percent = round((zone.current_count / zone.max_capacity) * 100, 1) if zone.max_capacity else 0
    zone.is_full = zone.current_count >= zone.max_capacity
    return zone


@router.put("/occupancy/{zone_id}/capacity", summary="Set max capacity for a zone")
def set_zone_capacity(zone_id: str, body: ZoneCapacityUpdate, db: Session = Depends(get_db)):
    """
    Update the maximum vehicle capacity for a zone.
    Call this once per zone during system setup.
    """
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        zone = ZoneOccupancy(zone_id=zone_id, camera_id="manual",
                             current_count=0, max_capacity=body.max_capacity,
                             last_updated=datetime.utcnow())
        db.add(zone)
    else:
        zone.max_capacity = body.max_capacity
    db.commit()
    return {"zone_id": zone_id, "max_capacity": body.max_capacity, "status": "updated"}


@router.put("/occupancy/{zone_id}/reset", summary="Reset zone count to zero")
def reset_zone_count(zone_id: str, db: Session = Depends(get_db)):
    """Manually reset zone vehicle count. Use after system restart or miscounts."""
    zone = db.query(ZoneOccupancy).filter(ZoneOccupancy.zone_id == zone_id).first()
    if not zone:
        raise HTTPException(status_code=404, detail=f"Zone '{zone_id}' not found")
    zone.current_count = 0
    zone.last_updated = datetime.utcnow()
    db.commit()
    return {"zone_id": zone_id, "current_count": 0, "status": "reset"}
```

---

### 11.8 `app/main.py` (Final — with middleware + error handlers)

```python
# app/main.py
"""
FastAPI application entry point.
Includes security middleware, global error handlers, and all routers.
"""

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from app.routers import events, occupancy, violations, intrusion, health
from app.database import create_tables
from app.config import settings
from app.utils.logger import get_logger
import time

# Phase 2 routers — uncomment when ANPR cameras are installed
# from app.routers import entry_exit, parking_stats, vehicles

logger = get_logger(__name__)

app = FastAPI(
    title="Damanat Parking Analytics API",
    description="AI Camera event processing — Phase 1 + Phase 2. Fully offline.",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS (allow dashboard on same LAN to call the API) ──────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Restrict to dashboard IP in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Key Middleware ───────────────────────────────────────────────────────
class APIKeyMiddleware(BaseHTTPMiddleware):
    """
    Optional lightweight API key auth for non-camera endpoints.
    Camera webhook (/api/v1/events/camera) is excluded — cameras don't send keys.
    Set API_KEY in .env. Leave empty to disable auth.
    """
    async def dispatch(self, request: Request, call_next):
        # Always allow camera webhook and health check without auth
        open_paths = {"/api/v1/events/camera", "/api/v1/health", "/docs", "/redoc", "/openapi.json"}
        if request.url.path in open_paths or not settings.API_KEY:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if api_key != settings.API_KEY:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid or missing API key"},
            )
        return await call_next(request)


if settings.API_KEY:
    app.add_middleware(APIKeyMiddleware)


# ── Request Timing Middleware ────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = round((time.time() - start) * 1000, 2)
    logger.debug(f"{request.method} {request.url.path} → {response.status_code} ({duration}ms)")
    return response


# ── Global Exception Handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception on {request.url.path}: {exc}", exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"},
    )


# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(events.router,     prefix="/api/v1", tags=["📡 Camera Events"])
app.include_router(occupancy.router,  prefix="/api/v1", tags=["🅿️  Occupancy — UC3"])
app.include_router(violations.router, prefix="/api/v1", tags=["🚨 Violations — UC5"])
app.include_router(intrusion.router,  prefix="/api/v1", tags=["🔒 Intrusion — UC6"])
app.include_router(health.router,     prefix="/api/v1", tags=["💚 Health"])

# Phase 2 — uncomment when ANPR cameras are installed
# app.include_router(entry_exit.router,    prefix="/api/v1", tags=["🚗 Entry/Exit — UC1"])
# app.include_router(parking_stats.router, prefix="/api/v1", tags=["📊 Stats — UC2"])
# app.include_router(vehicles.router,      prefix="/api/v1", tags=["🔍 Vehicles — UC4"])


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    logger.info("🚀 Damanat Backend starting up...")
    create_tables()
    logger.info("✅ Database tables ready")
    logger.info(f"📡 Cameras configured: {list(settings.CAMERAS.keys())}")
    logger.info(f"🌐 Listening on http://{settings.BACKEND_IP}:{settings.BACKEND_PORT}")
    logger.info("📖 API docs at /docs")


@app.on_event("shutdown")
async def shutdown():
    logger.info("🛑 Damanat Backend shutting down...")
```

---

### 11.9 `app/config.py` (Final — with API key + all settings)

```python
# app/config.py
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://damanat:damanat@localhost:5432/damanat_db"

    # ── Network ───────────────────────────────────────────────────────────
    BACKEND_IP: str = "192.168.1.50"
    BACKEND_PORT: int = 8080

    # ── Security ──────────────────────────────────────────────────────────
    API_KEY: Optional[str] = None   # Set in .env to enable auth on API endpoints

    # ── Cameras ───────────────────────────────────────────────────────────
    CAMERAS: dict = {
        "CAM-01":    {"ip": "192.168.1.101", "user": "admin", "password": "CHANGE_ME", "phase": 1},
        "CAM-02":    {"ip": "192.168.1.102", "user": "admin", "password": "CHANGE_ME", "phase": 1},
        "CAM-03":    {"ip": "192.168.1.103", "user": "admin", "password": "CHANGE_ME", "phase": 1},
        "CAM-ENTRY": {"ip": "192.168.1.104", "user": "admin", "password": "CHANGE_ME", "phase": 2, "gate": "entry"},
        "CAM-EXIT":  {"ip": "192.168.1.105", "user": "admin", "password": "CHANGE_ME", "phase": 2, "gate": "exit"},
    }

    CAMERA_IP_MAP: dict = {
        "192.168.1.101": "CAM-01",
        "192.168.1.102": "CAM-02",
        "192.168.1.103": "CAM-03",
        "192.168.1.104": "CAM-ENTRY",
        "192.168.1.105": "CAM-EXIT",
    }

    # ── Thresholds ────────────────────────────────────────────────────────
    OCCUPANCY_ALERT_THRESHOLD: float = 0.90     # Alert at 90% full
    INTRUSION_COOLDOWN_SECONDS: int = 30         # Suppress re-alerts within 30s

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
```

---

### 11.10 `.env.example` (Complete)

```env
# ── Database ────────────────────────────────────────────────────────────────
DATABASE_URL=postgresql://damanat:damanat@localhost:5432/damanat_db

# ── Network ─────────────────────────────────────────────────────────────────
BACKEND_IP=192.168.1.50
BACKEND_PORT=8080

# ── Security ────────────────────────────────────────────────────────────────
# Set a strong random string to protect API endpoints (cameras are excluded)
# Leave empty to disable API auth during development
API_KEY=your-secret-api-key-here

# ── Logging ─────────────────────────────────────────────────────────────────
LOG_LEVEL=INFO

# ── Camera Credentials ──────────────────────────────────────────────────────
# Update these with real camera passwords after changing defaults
# CAM_01_PASSWORD=your-cam-01-password
# CAM_02_PASSWORD=your-cam-02-password
# CAM_03_PASSWORD=your-cam-03-password
```

---

### 11.11 `docker-compose.yml`

```yaml
# docker-compose.yml
# Starts PostgreSQL + the FastAPI backend together.
# Usage: docker-compose up -d

version: "3.9"

services:

  db:
    image: postgres:16-alpine
    container_name: damanat_db
    restart: unless-stopped
    environment:
      POSTGRES_USER: damanat
      POSTGRES_PASSWORD: damanat
      POSTGRES_DB: damanat_db
    ports:
      - "5432:5432"
    volumes:
      - db_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U damanat"]
      interval: 10s
      timeout: 5s
      retries: 5

  backend:
    build: .
    container_name: damanat_backend
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    ports:
      - "8080:8080"
    volumes:
      - ./logs:/app/logs
      - ./.env:/app/.env
    environment:
      DATABASE_URL: postgresql://damanat:damanat@db:5432/damanat_db
    command: uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 2

volumes:
  db_data:
```

---

### 11.12 `Dockerfile`

```dockerfile
# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create logs directory
RUN mkdir -p logs

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
```

---

### 11.13 `scripts/setup/init_db.py`

```python
# scripts/setup/init_db.py
"""
Initialize database — creates all tables.
Run once before first launch, or after adding new models.
Usage: python scripts/setup/init_db.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.database import create_tables, engine
from app.config import settings
from sqlalchemy import text


def main():
    print("🗄️  Damanat DB Initialization")
    print("=" * 40)
    print(f"📡 Database: {settings.DATABASE_URL}")

    # Test connection
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        print("✅ Database connection OK")
    except Exception as e:
        print(f"❌ Cannot connect to database: {e}")
        print("\nMake sure PostgreSQL is running:")
        print("  docker-compose up -d db")
        print("  # or: sudo systemctl start postgresql")
        sys.exit(1)

    # Create all tables
    print("\n📋 Creating tables...")
    create_tables()
    print("✅ All tables created (Phase 1 + Phase 2)")

    # List created tables
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        ))
        tables = [row[0] for row in result]

    print(f"\n📊 Tables in database ({len(tables)} total):")
    for t in tables:
        print(f"   ✓ {t}")

    print("\n🎉 Database ready! You can now start the backend:")
    print("   uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload")


if __name__ == "__main__":
    main()
```

---

### 11.14 `scripts/test/test_camera_conn.py`

```python
# scripts/test/test_camera_conn.py
"""
Tests connectivity to all cameras by hitting their ISAPI deviceInfo endpoint.
Usage: python scripts/test/test_camera_conn.py
       python scripts/test/test_camera_conn.py --phase 1
       python scripts/test/test_camera_conn.py --phase 2
"""

import sys
import os
import argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import requests
from requests.auth import HTTPDigestAuth
import xml.etree.ElementTree as ET
from app.config import settings


def test_camera(cam_id: str, cam: dict) -> dict:
    url = f"http://{cam['ip']}/ISAPI/System/deviceInfo"
    try:
        resp = requests.get(
            url,
            auth=HTTPDigestAuth(cam["user"], cam["password"]),
            timeout=5,
        )
        if resp.status_code == 200:
            # Parse device info from XML
            try:
                root = ET.fromstring(resp.text)
                ns = "http://www.isapi.org/ver20/XMLSchema"
                def get(tag):
                    el = root.find(f"{{{ns}}}{tag}") or root.find(tag)
                    return el.text.strip() if el is not None and el.text else "N/A"
                model = get("model")
                serial = get("serialNumber")
                firmware = get("firmwareVersion")
            except:
                model, serial, firmware = "N/A", "N/A", "N/A"

            return {"status": "✅ online", "model": model, "serial": serial, "firmware": firmware}
        elif resp.status_code == 401:
            return {"status": "❌ auth_failed", "hint": "Wrong username or password"}
        else:
            return {"status": f"❌ http_{resp.status_code}"}

    except requests.exceptions.ConnectTimeout:
        return {"status": "❌ timeout", "hint": "Camera unreachable — check IP and network"}
    except requests.exceptions.ConnectionError:
        return {"status": "❌ connection_refused", "hint": "No device at this IP"}
    except Exception as e:
        return {"status": f"❌ error: {e}"}


def test_webhook_registered(cam_id: str, cam: dict) -> str:
    """Check if backend HTTP host is already configured on the camera."""
    url = f"http://{cam['ip']}/ISAPI/Event/notification/httpHosts"
    try:
        resp = requests.get(
            url,
            auth=HTTPDigestAuth(cam["user"], cam["password"]),
            timeout=5,
        )
        if resp.status_code == 200 and settings.BACKEND_IP in resp.text:
            return f"✅ webhook registered → {settings.BACKEND_IP}:{settings.BACKEND_PORT}"
        elif resp.status_code == 200:
            return "⚠️  webhook NOT configured (run configure_cameras.py)"
        else:
            return f"❓ cannot check ({resp.status_code})"
    except:
        return "❓ cannot check (camera offline)"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["1", "2", "all"], default="all")
    args = parser.parse_args()
    target_phases = {1, 2} if args.phase == "all" else {int(args.phase)}

    print("📷 Damanat Camera Connectivity Test")
    print("=" * 55)

    all_ok = True
    for cam_id, cam in settings.CAMERAS.items():
        if cam["phase"] not in target_phases:
            continue

        print(f"\n[{cam_id}] Phase {cam['phase']} — {cam['ip']}")
        result = test_camera(cam_id, cam)

        print(f"  Status   : {result['status']}")
        if result["status"].startswith("✅"):
            print(f"  Model    : {result.get('model', 'N/A')}")
            print(f"  Serial   : {result.get('serial', 'N/A')}")
            print(f"  Firmware : {result.get('firmware', 'N/A')}")
            webhook = test_webhook_registered(cam_id, cam)
            print(f"  Webhook  : {webhook}")
        else:
            all_ok = False
            if "hint" in result:
                print(f"  Hint     : {result['hint']}")

    print("\n" + "=" * 55)
    if all_ok:
        print("✅ All cameras reachable and ready.")
    else:
        print("⚠️  Some cameras have issues. Fix them before starting the backend.")
        sys.exit(1)


if __name__ == "__main__":
    main()
```

---

### 11.15 `README.md`

```markdown
# Damanat Parking Analytics Backend

Fully offline AI camera event processing system for Damanat parking facility (Saudi Arabia).  
Phase 1: Intrusion, Occupancy, Violations | Phase 2: ANPR Entry/Exit, Parking Duration

## Quick Start

### 1. Prerequisites
- Python 3.11+
- PostgreSQL 14+ (or Docker)
- Hikvision cameras on local network

### 2. Setup
```bash
git clone <repo-url> && cd damanat-backend
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Edit with real IPs + DB URL
```

### 3. Start Database
```bash
# Option A: Docker
docker-compose up -d db

# Option B: Local PostgreSQL
sudo systemctl start postgresql
createdb damanat_db
```

### 4. Initialize DB & Configure Cameras
```bash
python scripts/setup/init_db.py
python scripts/test/test_camera_conn.py      # Verify cameras are reachable
python scripts/setup/configure_cameras.py --phase 1   # Register backend on cameras
```

### 5. Start Backend
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
# API docs: http://192.168.1.50:8080/docs
```

## Phase 2 Activation (when ANPR cameras arrive)
```bash
python scripts/setup/configure_cameras.py --phase 2
python scripts/setup/configure_anpr_cameras.py
# Uncomment Phase 2 routers in app/main.py
```

## API Endpoints
See `/docs` for full Swagger UI.

| Endpoint | Description |
|----------|-------------|
| POST /api/v1/events/camera | Camera webhook (all events) |
| GET  /api/v1/occupancy     | Zone occupancy (UC3) |
| GET  /api/v1/violations    | Violation alerts (UC5) |
| GET  /api/v1/intrusions    | Intrusion alerts (UC6) |
| GET  /api/v1/health        | System health |

## Project Structure
See `DAMANAT-SYSTEM-PROMPT.md` for full documentation.
```

---

### 11.16 Complete `requirements.txt`

```
# Web framework
fastapi==0.110.0
uvicorn[standard]==0.29.0
starlette==0.36.3
python-multipart==0.0.9

# Database
sqlalchemy==2.0.28
psycopg2-binary==2.9.9
alembic==1.13.1

# Config & validation
pydantic==2.6.3
pydantic-settings==2.2.1
python-dotenv==1.0.1

# XML / HTTP
lxml==5.1.0
requests==2.31.0
httpx==0.27.0

# Testing
pytest==8.1.0
pytest-asyncio==0.23.5
httpx==0.27.0
```

---

## 12. Final Completion Checklist

Before handing the project to the AI model or the team, verify:

### Infrastructure
- [ ] `app/database.py` — ✅ Section 11.1
- [ ] `app/config.py` — ✅ Section 11.9
- [ ] `app/utils/logger.py` — ✅ Section 11.2
- [ ] `app/utils/xml_parser.py` — ✅ Section 11.3
- [ ] `app/utils/json_parser.py` — ✅ Section 11.4
- [ ] `docker-compose.yml` — ✅ Section 11.11
- [ ] `Dockerfile` — ✅ Section 11.12
- [ ] `.env.example` — ✅ Section 11.10
- [ ] `README.md` — ✅ Section 11.15
- [ ] `requirements.txt` — ✅ Section 11.16

### Application
- [ ] `app/main.py` (with middleware + error handlers) — ✅ Section 11.8
- [ ] `app/routers/events.py` — ✅ Section 7.1
- [ ] `app/routers/health.py` — ✅ Section 11.6
- [ ] `app/routers/occupancy.py` (with capacity management) — ✅ Section 11.7
- [ ] `app/routers/violations.py` — ✅ Section 7.5
- [ ] `app/routers/intrusion.py` — ✅ Section 7.6
- [ ] `app/routers/entry_exit.py` 🔜 Phase 2 — ✅ Section 8.2
- [ ] `app/routers/parking_stats.py` 🔜 Phase 2 — ✅ Section 8.3
- [ ] `app/routers/vehicles.py` 🔜 Phase 2 — ✅ Section 8.4

### Services
- [ ] `app/services/event_parser.py` — ✅ Section 7.2
- [ ] `app/services/event_dispatcher.py` — ✅ Section 7.3
- [ ] `app/services/occupancy_service.py` — ✅ Section 7.4
- [ ] `app/services/violation_service.py` — ✅ Section 7.5
- [ ] `app/services/intrusion_service.py` — ✅ Section 7.6
- [ ] `app/services/alert_service.py` — ✅ Section 7.7
- [ ] `app/services/entry_exit_service.py` 🔜 Phase 2 — ✅ Section 8.1

### Models & Schemas
- [ ] `app/models/camera_event.py` — ✅ Section 6
- [ ] `app/models/zone_occupancy.py` — ✅ Section 6
- [ ] `app/models/alert.py` — ✅ Section 6
- [ ] `app/models/vehicle.py` 🔜 Phase 2 — ✅ Section 6
- [ ] `app/models/entry_exit_log.py` 🔜 Phase 2 — ✅ Section 6
- [ ] `app/schemas/camera_event.py` — ✅ Section 11.5
- [ ] `app/schemas/zone_occupancy.py` — ✅ Section 11.5
- [ ] `app/schemas/alert.py` — ✅ Section 11.5
- [ ] `app/schemas/vehicle.py` — ✅ Section 11.5
- [ ] `app/schemas/entry_exit_log.py` — ✅ Section 11.5

### Scripts
- [ ] `scripts/setup/configure_cameras.py` — ✅ Section 4.1
- [ ] `scripts/setup/configure_anpr_cameras.py` 🔜 Phase 2 — ✅ Section 4.2
- [ ] `scripts/setup/init_db.py` — ✅ Section 11.13
- [ ] `scripts/test/simulate_event.py` — ✅ Section 8.5
- [ ] `scripts/test/test_camera_conn.py` — ✅ Section 11.14

---

**Estimated AI completion using this document: 95–97%**  
Remaining 3–5%: minor import wiring, `__init__.py` files, and Alembic migrations (optional).

---

*Document prepared by Clawdy (Claude Opus 4.6) — Damanat Project — February 2026*  
*Phase 1: DS-2CD3681G2 + DS-2CD3781G2 + DS-2CD3783G2 (AcuSense)*  
*Phase 2: ANPR LPR cameras at entry/exit gates*
