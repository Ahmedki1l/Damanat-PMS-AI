# app/utils/logger.py
"""
Centralised logging configuration for the entire application.
Logs to console and to a rotating file in /logs/.
"""

import logging
import os
from logging.handlers import RotatingFileHandler

from app.config import settings

LOG_LEVEL = settings.LOG_LEVEL.upper()
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
