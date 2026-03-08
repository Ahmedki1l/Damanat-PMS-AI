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

<<<<<<< HEAD
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
       
        # Phase 2 — Uncomment when ANPR cameras are installed
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
        # Phase 2 — Uncomment when ANPR cameras are installed
        "10.1.13.100": "CAM-ENTRY",
        "10.1.13.101": "CAM-EXIT",
    }
=======
    # ── Phase 1 Camera credentials (read from .env) ──────────────────────
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

    CAM_09_IP: str = ""
    CAM_09_USER: str = ""
    CAM_09_PASSWORD: str = ""
    CAM_09_NAME: str = "B2-PARKING"

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

    CAM_02_IP: str = ""
    CAM_02_USER: str = ""
    CAM_02_PASSWORD: str = ""
    CAM_02_NAME: str = "GF-WAITING"

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
            "CAM-04": (self.CAM_04_IP, self.CAM_04_USER, self.CAM_04_PASSWORD, self.CAM_04_NAME),
            "CAM-05": (self.CAM_05_IP, self.CAM_05_USER, self.CAM_05_PASSWORD, self.CAM_05_NAME),
            "CAM-06": (self.CAM_06_IP, self.CAM_06_USER, self.CAM_06_PASSWORD, self.CAM_06_NAME),
            "CAM-07": (self.CAM_07_IP, self.CAM_07_USER, self.CAM_07_PASSWORD, self.CAM_07_NAME),
            "CAM-09": (self.CAM_09_IP, self.CAM_09_USER, self.CAM_09_PASSWORD, self.CAM_09_NAME),
            "CAM-11": (self.CAM_11_IP, self.CAM_11_USER, self.CAM_11_PASSWORD, self.CAM_11_NAME),
            "CAM-12": (self.CAM_12_IP, self.CAM_12_USER, self.CAM_12_PASSWORD, self.CAM_12_NAME),
            "CAM-13": (self.CAM_13_IP, self.CAM_13_USER, self.CAM_13_PASSWORD, self.CAM_13_NAME),
            "CAM-14": (self.CAM_14_IP, self.CAM_14_USER, self.CAM_14_PASSWORD, self.CAM_14_NAME),
            "CAM-02": (self.CAM_02_IP, self.CAM_02_USER, self.CAM_02_PASSWORD, self.CAM_02_NAME),
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

        self.CAMERAS = cameras
        self.CAMERA_IP_MAP = ip_map
        return self

    # ── Zone configuration ───────────────────────────────────────────────
    # Comma-separated zone IDs. Override in .env.
    RESTRICTED_ZONES: str = "restricted-vip,no-parking-zone,emergency-exit,loading-bay"
    MONITORED_INTRUSION_ZONES: str = "emergency-exit,staff-only-area,after-hours-zone"
    ALWAYS_VIOLATION_EVENTS: str = "linedetection"
>>>>>>> origin/Amr

    # ── Thresholds ────────────────────────────────────────────────────────
    OCCUPANCY_ALERT_THRESHOLD: float = 0.90     # Alert at 90% full
    DEFAULT_ZONE_CAPACITY: int = 50             # Default max_capacity for auto-created zones
    INTRUSION_COOLDOWN_SECONDS: int = 30         # Suppress re-alerts within 30s
    VIOLATION_COOLDOWN_SECONDS: int = 30         # Suppress re-alerts within 30s

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
