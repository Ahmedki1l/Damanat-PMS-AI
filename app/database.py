# app/database.py
"""
Database connection, session management, and table creation.
Uses SQLAlchemy with PostgreSQL. All models are auto-imported here
so create_tables() creates every table in one call.
"""

import os as _os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool as _NullPool
from app.config import settings

_is_serverless = _os.environ.get("VERCEL") == "1"

engine = create_engine(
    settings.db_url,
    pool_pre_ping=True,          # Auto-reconnect if DB connection drops
    poolclass=_NullPool if _is_serverless else None,
    **({} if _is_serverless else {"pool_size": 10, "max_overflow": 20}),
    echo=False,                  # Set True to log all SQL queries (debug only)
)

if engine.dialect.name == "mssql":
    from sqlalchemy import event as _event

    @_event.listens_for(engine, "connect")
    def _set_lock_timeout(dbapi_conn, _record):
        """Never let a blocked query wait forever.

        pyodbc is synchronous and runs on the asyncio event loop, so a query that
        waits indefinitely on a lock does not just stall one request — it freezes
        the entire service. Bounding the wait turns that permanent freeze into a
        normal, logged error (SQL Server 1222) that the handler can roll back.
        """
        cur = dbapi_conn.cursor()
        cur.execute(f"SET LOCK_TIMEOUT {settings.DB_LOCK_TIMEOUT_MS}")
        cur.close()


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
    from app.models.zone_occupancy import ZoneOccupancy   # noqa
    from app.models.alert import Alert                     # noqa
    # Phase 2 models
    from app.models.vehicle import Vehicle                 # noqa
    from app.models.entry_exit_log import EntryExitLog     # noqa
    from app.models.parking_session import ParkingSession  # noqa
    from app.models.camera_feed import CameraFeed
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

