# app/config.py
"""
Application configuration using Pydantic-Settings.
All settings can be overridden via environment variables or .env file.
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ── Database ──────────────────────────────────────────────────────────
    DATABASE_URL: str = "postgresql://damanat:damanat@localhost:5432/damanat_db"

    # ── Network ───────────────────────────────────────────────────────────
    BACKEND_IP: str = "5.5.5.3"
    BACKEND_PORT: int = 8080

    # ── Security ──────────────────────────────────────────────────────────
    API_KEY: Optional[str] = None   # Set in .env to enable auth on API endpoints

    # ── Cameras ───────────────────────────────────────────────────────────
    # Phase 1 — Active
    CAMERAS: dict = {
        "CAM-04":  {"ip": "10.1.13.63", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-PARKING"},
        "CAM-02":  {"ip": "10.1.13.20", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "GF-WAITING"},
        "CAM-35":  {"ip": "10.1.13.54", "user": "kloudspot", "password": "Kloud@123", "phase": 1, "name": "B1-DATA CENTER"},
        # Phase 2 — Uncomment when ANPR cameras are installed
        # "CAM-ENTRY": {"ip": "x.x.x.x", "user": "kloudspot", "password": "Kloud@123", "phase": 2, "gate": "entry"},
        # "CAM-EXIT":  {"ip": "x.x.x.x", "user": "kloudspot", "password": "Kloud@123", "phase": 2, "gate": "exit"},
    }

    CAMERA_IP_MAP: dict = {
        # Phase 1
        "10.1.13.63": "CAM-04",
        "10.1.13.20": "CAM-02",
        "10.1.13.54": "CAM-35",
        # Phase 2 — Uncomment when ANPR cameras are installed
        # "x.x.x.x": "CAM-ENTRY",
        # "x.x.x.x": "CAM-EXIT",
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
