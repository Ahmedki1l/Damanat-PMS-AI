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

    # ── Camera Credentials ────────────────────────────────────────────────
    # The values below are generic DEFAULTS. Real data is loaded from .env.
    CAMERA_USER: str = "admin"
    CAM_02_IP: str = "0.0.0.0"
    CAM_02_PASSWORD: str = "CHANGE_ME"
    CAM_04_IP: str = "0.0.0.0"
    CAM_04_PASSWORD: str = "CHANGE_ME"
    CAM_35_IP: str = "0.0.0.0"
    CAM_35_PASSWORD: str = "CHANGE_ME"
    CAM_ENTRY_IP: str = "0.0.0.0"
    CAM_ENTRY_PASSWORD: str = "CHANGE_ME"
    CAM_EXIT_IP: str = "0.0.0.0"
    CAM_EXIT_PASSWORD: str = "CHANGE_ME"

    # ── Cameras ───────────────────────────────────────────────────────────
    @property
    def CAMERAS(self) -> dict:
        return {
            "CAM-02":    {"ip": self.CAM_02_IP, "user": self.CAMERA_USER, "password": self.CAM_02_PASSWORD, "phase": 1},
            "CAM-04":    {"ip": self.CAM_04_IP, "user": self.CAMERA_USER, "password": self.CAM_04_PASSWORD, "phase": 1},
            "CAM-35":    {"ip": self.CAM_35_IP, "user": self.CAMERA_USER, "password": self.CAM_35_PASSWORD, "phase": 1},
            #"CAM-ENTRY": {"ip": self.CAM_ENTRY_IP, "user": self.CAMERA_USER, "password": self.CAM_ENTRY_PASSWORD, "phase": 2, "gate": "entry"},
            #"CAM-EXIT":  {"ip": self.CAM_EXIT_IP, "user": self.CAMERA_USER, "password": self.CAM_EXIT_PASSWORD, "phase": 2, "gate": "exit"},
        }

    @property
    def CAMERA_IP_MAP(self) -> dict:
        return {
            self.CAM_02_IP: "CAM-02",
            self.CAM_04_IP: "CAM-04",
            self.CAM_35_IP: "CAM-35",
            #self.CAM_ENTRY_IP: "CAM-ENTRY",
            #self.CAM_EXIT_IP: "CAM-EXIT",
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
