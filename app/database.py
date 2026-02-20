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
