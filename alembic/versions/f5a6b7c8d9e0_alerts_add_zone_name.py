"""alerts: add zone_name column and backfill zone_name + slot_number

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-03-13 21:16:00.000000

Changes:
  1. Add alerts.zone_name (VARCHAR 100, nullable) — explicit stored column replacing the
     former @property.  Backfilled from zone_id for all existing rows.
  2. Backfill alerts.slot_number using ZONE_REAL_SLOT config for rows that have
     region_id but slot_number IS NULL.
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "f5a6b7c8d9e0"
down_revision: Union[str, Sequence[str], None] = "e4f5a6b7c8d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("alerts")}

    # ── 1. Add zone_name column ──────────────────────────────────────────────
    if "zone_name" not in columns:
        op.add_column("alerts", sa.Column("zone_name", sa.String(100), nullable=True))

    # ── 2. Backfill zone_name = zone_id for all existing rows ───────────────
    conn.execute(text("UPDATE alerts SET zone_name = zone_id WHERE zone_name IS NULL"))

    # ── 3. Backfill slot_number using ZONE_REAL_SLOT config ─────────────────
    #   We import at runtime so the migration stays self-contained.
    try:
        from app.zone_config import get_real_slot_number

        rows = conn.execute(
            text(
                "SELECT id, camera_id, region_id FROM alerts "
                "WHERE slot_number IS NULL AND region_id IS NOT NULL"
            )
        ).fetchall()

        for row in rows:
            slot = get_real_slot_number(row.camera_id, str(row.region_id))
            if slot is not None:
                conn.execute(
                    text("UPDATE alerts SET slot_number = :slot WHERE id = :id"),
                    {"slot": slot, "id": row.id},
                )

        if rows:
            print(f"[migration] Backfilled slot_number on {len(rows)} alert(s).")
    except Exception as exc:
        # Non-fatal: slot_number backfill is best-effort
        print(f"[migration] slot_number backfill skipped: {exc}")


def downgrade() -> None:
    op.drop_column("alerts", "zone_name")
