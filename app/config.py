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
