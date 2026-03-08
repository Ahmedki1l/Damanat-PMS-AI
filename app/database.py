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
    Kept for scripts (init_db.py) that don't need full Alembic.
    """
    # Phase 1 models
    from app.models.camera_event import CameraEvent       # noqa
    from app.models.zone_occupancy import ZoneOccupancy   # noqa
    from app.models.alert import Alert                     # noqa
    # Phase 2 models
    from app.models.vehicle import Vehicle                 # noqa
    from app.models.entry_exit_log import EntryExitLog     # noqa

    Base.metadata.create_all(bind=engine)


def run_migrations():
    """Run Alembic migrations using the app's existing engine.

    Runs migrations directly on the app engine instead of letting env.py
    create a separate engine, which avoids blocking issues in async startup.
    The migration itself is idempotent (checks for existing tables).
    """
    from alembic.config import Config
    from alembic import command
    from alembic.runtime.migration import MigrationContext
    from alembic.runtime.environment import EnvironmentContext
    from alembic.script import ScriptDirectory
    import os

    alembic_cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    script = ScriptDirectory.from_config(alembic_cfg)

    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        current_rev = context.get_current_revision()
        head_rev = script.get_current_head()

        if current_rev == head_rev:
            return  # Already at head, nothing to do

        # Run upgrade within this connection
        def do_upgrade(rev, context):
            return script._upgrade_revs(head_rev, rev)

        with EnvironmentContext(alembic_cfg, script, fn=do_upgrade) as env_ctx:
            env_ctx.configure(
                connection=conn,
                target_metadata=Base.metadata,
            )
            with env_ctx.begin_transaction():
                env_ctx.run_migrations()

        conn.commit()
