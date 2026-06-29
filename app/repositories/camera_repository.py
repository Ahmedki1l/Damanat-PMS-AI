# app/repositories/camera_repository.py
"""
Camera inventory data access (read-only).

The `cameras` table is owned and provisioned by the API Gateway (Prisma),
not by PMS-AI's Alembic migrations — same convention as `parking_slots` read
in app/services/occupancy_service.py. We therefore read it with raw SQL via
`text()` rather than a SQLAlchemy model (a model on `Base` would get picked up
by `create_tables()` / `Base.metadata.create_all` and clash with the existing
gateway-managed table).
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


# Only the columns the loader needs. `enabled = 1` filters out cameras the
# operator has disabled in the dashboard so we don't ping/credential them.
_SELECT_ENABLED_CAMERAS = text(
    """
    SELECT camera_id, name, floor, zone_id, ip_address, username, password_encrypted
    FROM cameras
    WHERE enabled = 1
    """
)


def fetch_enabled_camera_rows(db: Session):
    """Return all enabled camera rows. Raises on DB/connectivity errors — the
    caller (camera_loader) is responsible for catching and falling back to the
    .env-built inventory."""
    return db.execute(_SELECT_ENABLED_CAMERAS).fetchall()
