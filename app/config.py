# app/config.py
"""
Application configuration using Pydantic-Settings.
All settings can be overridden via environment variables or .env file.
"""

from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import Optional


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://damanat:damanat@localhost:5432/damanat_db"

    # ── Network ───────────────────────────────────────────────────────────
    BACKEND_IP: str = "5.5.5.1"
    BACKEND_PORT: int = 8080

    # ── Security ──────────────────────────────────────────────────────────
    API_KEY: Optional[str] = None   # Set in .env to enable auth on API endpoints

    # ── Cameras ───────────────────────────────────────────────────────────
    # Phase 1 — Active
    CAMERAS: dict = {

        #"CAM-35":  {"ip": "10.1.13.54", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-DATA CENTER"},
        "CAM-14":  {"ip": "10.1.13.73", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        "CAM-13":  {"ip": "10.1.13.72", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        "CAM-12":  {"ip": "10.1.13.71", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        "CAM-11":  {"ip": "10.1.13.70", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        "CAM-10":  {"ip": "10.1.13.69", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        "CAM-09":  {"ip": "10.1.13.68", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        #"CAM-08":  {"ip": "10.1.13.67", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B2-PARKING"},
        "CAM-07":  {"ip": "10.1.13.66", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-PARKING"},
        "CAM-06":  {"ip": "10.1.13.65", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-PARKING"},
        "CAM-05":  {"ip": "10.1.13.64", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-PARKING"},
        "CAM-04":  {"ip": "10.1.13.63", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-PARKING"},
        #"CAM-03":  {"ip": "10.1.13.62", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-PARKING"},
        "CAM-02":  {"ip": "10.1.13.20", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "GF-WAITING"},

        # Phase 2 — ANPR cameras
        "CAM-ENTRY": {"ip": "10.1.13.100", "user": "kloudspott", "password": "Kloudspot@321", "phase": 2, "gate": "entry"},
        "CAM-EXIT":  {"ip": "10.1.13.101", "user": "kloudspot", "password": "Kloudspot@321", "phase": 2, "gate": "exit"},
    }

    CAMERA_IP_MAP: dict = {
        # Phase 1
        "10.1.13.63": "CAM-04",
        "10.1.13.20": "CAM-02",
        "10.1.13.54": "CAM-35",
        "10.1.13.73": "CAM-14",
        "10.1.13.72": "CAM-13",
        "10.1.13.71": "CAM-12",
        "10.1.13.70": "CAM-11",
        "10.1.13.69": "CAM-10",
        "10.1.13.68": "CAM-09",
        "10.1.13.67": "CAM-08",
        "10.1.13.66": "CAM-07",
        "10.1.13.65": "CAM-06",
        "10.1.13.64": "CAM-05",
        "10.1.13.62": "CAM-03",
        # Phase 2
        "10.1.13.100": "CAM-ENTRY",
        "10.1.13.101": "CAM-EXIT",
    }

    # ── Zone configuration ───────────────────────────────────────────────
    # Comma-separated zone IDs. Override in .env.
    RESTRICTED_ZONES: str = "restricted-vip,no-parking-zone,emergency-exit,loading-bay"
    MONITORED_INTRUSION_ZONES: str = "emergency-exit,staff-only-area,after-hours-zone"
    ALWAYS_VIOLATION_EVENTS: str = "linedetection"

    # ── Thresholds ────────────────────────────────────────────────────────
    OCCUPANCY_ALERT_THRESHOLD: float = 0.90     # Alert at 90% full
    DEFAULT_ZONE_CAPACITY: int = 50             # Default max_capacity for auto-created zones
    INTRUSION_COOLDOWN_SECONDS: int = 30         # Suppress re-alerts within 30s
    VIOLATION_COOLDOWN_SECONDS: int = 30         # Suppress re-alerts within 30s

    # ── Event Stream Deduplication ─────────────────────────────────────────
    EVENT_STREAM_SUPPRESS_SECONDS: int = 30      # Suppress duplicate events within this window
    EVENT_STREAM_MAX_DURATION_SECONDS: int = 300  # Reset session after this elapsed time

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
