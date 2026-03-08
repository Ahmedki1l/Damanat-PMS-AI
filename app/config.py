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
    BACKEND_IP: str = "5.5.5.3"
    BACKEND_PORT: int = 8080

    # ── Security ──────────────────────────────────────────────────────────
    API_KEY: Optional[str] = None   # Set in .env to enable auth on API endpoints

    # ── Camera credentials (read from .env) ───────────────────────────────
    # Phase 1
    CAM_04_IP: str = ""
    CAM_04_USER: str = ""
    CAM_04_PASSWORD: str = ""
    CAM_04_NAME: str = "B1-PARKING"

    CAM_02_IP: str = ""
    CAM_02_USER: str = ""
    CAM_02_PASSWORD: str = ""
    CAM_02_NAME: str = "GF-WAITING"

    CAM_35_IP: str = ""
    CAM_35_USER: str = ""
    CAM_35_PASSWORD: str = ""
    CAM_35_NAME: str = "B1-DATA CENTER"

    # Phase 2 — Set these in .env when ANPR cameras are installed
    CAM_ENTRY_IP: str = ""
    CAM_ENTRY_USER: str = ""
    CAM_ENTRY_PASSWORD: str = ""

    CAM_EXIT_IP: str = ""
    CAM_EXIT_USER: str = ""
    CAM_EXIT_PASSWORD: str = ""

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
                "password": self.CAM_ENTRY_PASSWORD, "phase": 2, "gate": "entry",
            }
            ip_map[self.CAM_ENTRY_IP] = "CAM-ENTRY"

        if self.CAM_EXIT_IP:
            cameras["CAM-EXIT"] = {
                "ip": self.CAM_EXIT_IP, "user": self.CAM_EXIT_USER,
                "password": self.CAM_EXIT_PASSWORD, "phase": 2, "gate": "exit",
            }
            ip_map[self.CAM_EXIT_IP] = "CAM-EXIT"

        self.CAMERAS = cameras
        self.CAMERA_IP_MAP = ip_map
        return self

    # ── Thresholds ────────────────────────────────────────────────────────
    OCCUPANCY_ALERT_THRESHOLD: float = 0.90     # Alert at 90% full
    INTRUSION_COOLDOWN_SECONDS: int = 30         # Suppress re-alerts within 30s

    # ── Logging ───────────────────────────────────────────────────────────
    LOG_LEVEL: str = "INFO"

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
