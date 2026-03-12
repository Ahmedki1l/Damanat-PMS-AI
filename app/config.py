# app/config.py
"""
Application configuration using Pydantic-Settings.
All settings can be overridden via environment variables or .env file.
"""

from pydantic_settings import BaseSettings
from pydantic import model_validator
from typing import Optional, Dict, List


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://damanat:damanat@localhost:5432/damanat_db"

    @property
    def db_url(self) -> str:
        """Return DATABASE_URL with correct prefix for SQLAlchemy.
        Render provides postgres:// — SQLAlchemy 2.0 requires postgresql://."""
        return self.DATABASE_URL.replace("postgres://", "postgresql://", 1)

    # ── Network ───────────────────────────────────────────────────────────
    BACKEND_IP: str = "5.5.5.3"
    BACKEND_PORT: int = 8080

    # ── Security ──────────────────────────────────────────────────────────
    API_KEY: Optional[str] = None   # Set in .env to enable auth on API endpoints

    # ── Node.js Core Backend Integration ─────────────────────────────────
    NODEBACK_URL: str = ""          # e.g. "http://localhost:3000"; empty = disabled
    NODEBACK_SITE_ID: str = ""      # UUID of this parking site in the website backend
    NODEBACK_SERVICE_KEY: str = ""  # X-Service-Key header value for service-to-service auth

    # ── Zone UUID mappings (from Node.js backend database) ────────────────
    # Camera ID → Zone UUID (used for alert zone_id in violation/intrusion/ANPR)
    CAMERA_ZONE_MAP: dict = {
        "CAM-03":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-04":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-05":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-06":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-07":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-08":    "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",  # B1-PARKING
        "CAM-09":    "93651f64-fb84-4082-b51e-9477cf7c06ac",  # B2-PARKING
        "CAM-11":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-12":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-13":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-14":    "93651f64-fb84-4082-b51e-9477cf7c06ac",
        "CAM-ENTRY": "b17a1403-2b37-4a03-8e49-0977a1d16736",  # GF-GATES
        "CAM-EXIT":  "b17a1403-2b37-4a03-8e49-0977a1d16736",  # GF-GATES
    }

    # Zone name → Zone UUID (used for occupancy HTTP push)
    # "GARAGE-TOTAL" is intentionally excluded — Node.js handles total counts internally
    ZONE_NAME_TO_UUID: dict = {
        "B1-PARKING":     "f33dd3d2-6fbd-4eda-b682-bd2a7d1f1061",
        "B2-PARKING":     "93651f64-fb84-4082-b51e-9477cf7c06ac",
    }
    # ── Phase 1 Camera credentials (read from .env) ──────────────────────
    CAM_03_IP: str = ""
    CAM_03_USER: str = ""
    CAM_03_PASSWORD: str = ""
    CAM_03_NAME: str = "B1-ENTRANCE-INTERNAL"

    CAM_04_IP: str = ""
    CAM_04_USER: str = ""
    CAM_04_PASSWORD: str = ""
    CAM_04_NAME: str = "B1-PARKING"

    CAM_05_IP: str = ""
    CAM_05_USER: str = ""
    CAM_05_PASSWORD: str = ""
    CAM_05_NAME: str = "B1-PARKING"

    CAM_06_IP: str = ""
    CAM_06_USER: str = ""
    CAM_06_PASSWORD: str = ""
    CAM_06_NAME: str = "B1-PARKING"

    CAM_07_IP: str = ""
    CAM_07_USER: str = ""
    CAM_07_PASSWORD: str = ""
    CAM_07_NAME: str = "B1-PARKING"

    CAM_08_IP: str = ""
    CAM_08_USER: str = ""
    CAM_08_PASSWORD: str = ""
    CAM_08_NAME: str = "B1-EXIT-INTERNAL"

    CAM_09_IP: str = ""
    CAM_09_USER: str = ""
    CAM_09_PASSWORD: str = ""
    CAM_09_NAME: str = "B2-PARKING"

    CAM_10_IP: str = ""
    CAM_10_USER: str = ""
    CAM_10_PASSWORD: str = ""
    CAM_10_NAME: str = "B2-PARKING"

    CAM_11_IP: str = ""
    CAM_11_USER: str = ""
    CAM_11_PASSWORD: str = ""
    CAM_11_NAME: str = "B2-PARKING"

    CAM_12_IP: str = ""
    CAM_12_USER: str = ""
    CAM_12_PASSWORD: str = ""
    CAM_12_NAME: str = "B2-PARKING"

    CAM_13_IP: str = ""
    CAM_13_USER: str = ""
    CAM_13_PASSWORD: str = ""
    CAM_13_NAME: str = "B2-PARKING"

    CAM_14_IP: str = ""
    CAM_14_USER: str = ""
    CAM_14_PASSWORD: str = ""
    CAM_14_NAME: str = "B2-PARKING"


    CAM_35_IP: str = ""
    CAM_35_USER: str = ""
    CAM_35_PASSWORD: str = ""
    CAM_35_NAME: str = "B1-DATA CENTER"

    # ── Phase 2 — ANPR cameras ───────────────────────────────────────────
    CAM_ENTRY_IP: str = ""
    CAM_ENTRY_USER: str = ""
    CAM_ENTRY_PASSWORD: str = ""
    CAM_ENTRY_NAME: str = "ENTRY-GATE"

    CAM_EXIT_IP: str = ""
    CAM_EXIT_USER: str = ""
    CAM_EXIT_PASSWORD: str = ""
    CAM_EXIT_NAME: str = "EXIT-GATE"

    # ── Derived camera dicts (built from env vars above) ──────────────────
    CAMERAS: dict = {}
    CAMERA_IP_MAP: dict = {}

    @model_validator(mode="after")
    def _build_camera_dicts(self) -> "Settings":
        """Build CAMERAS and CAMERA_IP_MAP from individual env vars."""
        cameras = {}
        ip_map = {}

        # Phase 1 cameras
        phase1_cams = {
            "CAM-03": (self.CAM_03_IP, self.CAM_03_USER, self.CAM_03_PASSWORD, self.CAM_03_NAME),
            "CAM-04": (self.CAM_04_IP, self.CAM_04_USER, self.CAM_04_PASSWORD, self.CAM_04_NAME),
            "CAM-05": (self.CAM_05_IP, self.CAM_05_USER, self.CAM_05_PASSWORD, self.CAM_05_NAME),
            "CAM-06": (self.CAM_06_IP, self.CAM_06_USER, self.CAM_06_PASSWORD, self.CAM_06_NAME),
            "CAM-07": (self.CAM_07_IP, self.CAM_07_USER, self.CAM_07_PASSWORD, self.CAM_07_NAME),
            "CAM-08": (self.CAM_08_IP, self.CAM_08_USER, self.CAM_08_PASSWORD, self.CAM_08_NAME),
            "CAM-09": (self.CAM_09_IP, self.CAM_09_USER, self.CAM_09_PASSWORD, self.CAM_09_NAME),
            "CAM-10": (self.CAM_10_IP, self.CAM_10_USER, self.CAM_10_PASSWORD, self.CAM_10_NAME),
            "CAM-11": (self.CAM_11_IP, self.CAM_11_USER, self.CAM_11_PASSWORD, self.CAM_11_NAME),
            "CAM-12": (self.CAM_12_IP, self.CAM_12_USER, self.CAM_12_PASSWORD, self.CAM_12_NAME),
            "CAM-13": (self.CAM_13_IP, self.CAM_13_USER, self.CAM_13_PASSWORD, self.CAM_13_NAME),
            "CAM-14": (self.CAM_14_IP, self.CAM_14_USER, self.CAM_14_PASSWORD, self.CAM_14_NAME),
            "CAM-35": (self.CAM_35_IP, self.CAM_35_USER, self.CAM_35_PASSWORD, self.CAM_35_NAME),
        }
        for cam_id, (ip, user, password, name) in phase1_cams.items():
            if ip:
                cameras[cam_id] = {
                    "ip": ip, "user": user, "password": password,
                    "phase": 1, "name": name,
                }
                ip_map[ip] = cam_id

        # Phase 2 cameras
        if self.CAM_ENTRY_IP:
            cameras["CAM-ENTRY"] = {
                "ip": self.CAM_ENTRY_IP, "user": self.CAM_ENTRY_USER,
                "password": self.CAM_ENTRY_PASSWORD, "phase": 2,
                "gate": "entry", "name": self.CAM_ENTRY_NAME,
            }
            ip_map[self.CAM_ENTRY_IP] = "CAM-ENTRY"

        if self.CAM_EXIT_IP:
            cameras["CAM-EXIT"] = {
                "ip": self.CAM_EXIT_IP, "user": self.CAM_EXIT_USER,
                "password": self.CAM_EXIT_PASSWORD, "phase": 2,
                "gate": "exit", "name": self.CAM_EXIT_NAME,
            }
            ip_map[self.CAM_EXIT_IP] = "CAM-EXIT"

        # Internal Floor-to-Floor and Main Entrance/Exit Internal Gates
        if "CAM-03" in cameras:
            cameras["CAM-03"]["gate"] = "entry"
        if "CAM-08" in cameras:
            cameras["CAM-08"]["gate"] = "exit"
        if "CAM-09" in cameras:
            cameras["CAM-09"]["gate"] = "entry"  # To B2
        if "CAM-10" in cameras:
            cameras["CAM-10"]["gate"] = "exit"   # From B2

        self.CAMERAS = cameras
        self.CAMERA_IP_MAP = ip_map
        return self

    # ── Zone configuration ───────────────────────────────────────────────
    RESTRICTED_ZONES: str = "restricted-vip,no-parking-zone,emergency-exit,loading-bay"
    MONITORED_INTRUSION_ZONES: str = "emergency-exit,staff-only-area,after-hours-zone"
    ALWAYS_VIOLATION_EVENTS: str = "linedetection"
    CAMERA_REGION_ZONE_MAP: str = ""  # e.g. CAM-04:0=emergency-exit;CAM-04:1=restricted-vip

    # ── Thresholds ────────────────────────────────────────────────────────
    OCCUPANCY_ALERT_THRESHOLD: float = 0.90
    DEFAULT_ZONE_CAPACITY: int = 50
    INTRUSION_COOLDOWN_SECONDS: int = 30
    VIOLATION_COOLDOWN_SECONDS: int = 30
    
    # False Exit Prevention (Bug #23)
    EXIT_CONFIRM_SECONDS: int = 5
    FORWARD_DIRECTION_FIELD: str = "B-to-A"  # Primary flow (entry/exit/transition)
    USE_EXIT_DIRECTION_VALIDATION: bool = True
    USE_EXIT_CONFIRM_WINDOW: bool = True

    # ── UC3: Occupancy Zone Config ───────────────────────────────────────
    GARAGE_TOTAL_ZONE: str = "GARAGE-TOTAL"
    B1_PARKING_ZONE: str = "B1-PARKING"
    B2_PARKING_ZONE: str = "B2-PARKING"

    OCCUPANCY_CAMERA_ID: str = "CAM-04"
    OCCUPANCY_ZONE_NAME: str = "B1-PARKING"  # Legacy/Default fallback
    
    # Internal Ground Truth Zones (Line detection IDs from CAM-09/CAM-10)
    OCCUPANCY_ENTRANCE_ZONES: List[str] = ["1"]
    OCCUPANCY_EXIT_ZONES: List[str] = ["2"]

    # ── Storage ───────────────────────────────────────────────────────────
    STORAGE_MODE: str = "local"          # "local" or "spaces"
    DO_SPACES_KEY: str = ""
    DO_SPACES_SECRET: str = ""
    DO_SPACES_ENDPOINT: str = ""         # e.g. sfo3.digitaloceanspaces.com
    DO_SPACES_BUCKET: str = ""
    DO_SPACES_REGION: str = ""
    DO_SPACES_CDN_URL: str = ""          # e.g. https://bucket.sfo3.cdn.digitaloceanspaces.com

    # ── Event Stream Deduplication ─────────────────────────────────────────
    EVENT_STREAM_SUPPRESS_SECONDS: int = 30      # Suppress duplicate events within this window
    EVENT_STREAM_MAX_DURATION_SECONDS: int = 300  # Reset session after this elapsed time

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"

    LOG_CAMERA_FILTER: str = ""
    LOG_CAMERA_EXCLUDE: str = ""


settings = Settings()