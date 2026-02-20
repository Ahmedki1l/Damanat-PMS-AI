# Changelog

All notable changes to this project will be documented in this file.

## [1.0.0] - 2026-02-20

### Added — Phase 1 (Current Cameras)
- **UC3: Parking Occupancy Monitoring** — Real-time zone vehicle counts via regionEntrance/regionExiting events from CAM-03
- **UC5: Proactive Violation Alerts** — Restricted zone enforcement + line crossing detection with cooldown dedup
- **UC6: Intrusion Detection** — Vehicle intrusion monitoring in designated zones
- **Event Processing Pipeline** — Unified event parser (XML + JSON), event dispatcher, alert service
- **API Endpoints** — 10 Phase 1 endpoints (events, occupancy, violations, intrusions, health)
- **Camera Configuration** — Automated webhook setup via ISAPI for all Hikvision cameras
- **Health Check** — DB connectivity + per-camera ISAPI ping
- **Security** — Optional API key middleware, CORS, request timing, global error handler
- **Infrastructure** — Docker (PostgreSQL 16 + FastAPI), rotating file logger, structured logging

### Added — Phase 2 (ANPR Cameras — Pre-built)
- **UC1: Entry/Exit Counting** — ANPR event logging with daily counts
- **UC2: Parking Duration** — Entry/exit matching with duration calculation
- **UC4: Vehicle Identity** — Vehicle CRUD, plate lookup, unknown vehicle alerts
- **ANPR Service** — Full AccessControllerEvent handler
- **8 Phase 2 API endpoints** — Commented out in main.py, ready to activate
