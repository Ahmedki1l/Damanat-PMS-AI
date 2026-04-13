"""add_parking_sessions_and_enrich_profiles

Revision ID: 4b53f82cd131
Revises: 3a42e71bc020
Create Date: 2026-04-11 12:35:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "4b53f82cd131"
down_revision: Union[str, Sequence[str], None] = "3a42e71bc020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {col["name"] for col in inspector.get_columns(table_name)}


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if "severity" not in _column_names("alerts"):
        op.add_column(
            "alerts",
            sa.Column("severity", sa.String(length=20), nullable=False, server_default="warning"),
        )
        op.alter_column("alerts", "severity", server_default=None)

    if "location_display" not in _column_names("alerts"):
        op.add_column(
            "alerts",
            sa.Column("location_display", sa.String(length=255), nullable=True),
        )

    vehicle_columns = _column_names("vehicles")
    if "is_employee" not in vehicle_columns:
        op.add_column(
            "vehicles",
            sa.Column("is_employee", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
        op.alter_column("vehicles", "is_employee", server_default=None)
    if "phone" not in vehicle_columns:
        op.add_column("vehicles", sa.Column("phone", sa.String(length=50), nullable=True))
    if "email" not in vehicle_columns:
        op.add_column("vehicles", sa.Column("email", sa.String(length=255), nullable=True))

    if not _table_exists("parking_sessions"):
        op.create_table(
            "parking_sessions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("plate_number", sa.String(length=50), nullable=False),
            sa.Column("vehicle_id", sa.Integer(), nullable=True),
            sa.Column("vehicle_type", sa.String(length=50), nullable=True),
            sa.Column("is_employee", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("entry_time", sa.DateTime(), nullable=False),
            sa.Column("exit_time", sa.DateTime(), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("entry_camera_id", sa.String(length=50), nullable=False),
            sa.Column("exit_camera_id", sa.String(length=50), nullable=True),
            sa.Column("entry_snapshot_path", sa.Text(), nullable=True),
            sa.Column("exit_snapshot_path", sa.Text(), nullable=True),
            sa.Column("floor", sa.String(length=50), nullable=True),
            sa.Column("zone_id", sa.String(length=100), nullable=True),
            sa.Column("zone_name", sa.String(length=100), nullable=True),
            sa.Column("slot_number", sa.String(length=100), nullable=True),
            sa.Column("parked_at", sa.DateTime(), nullable=True),
            sa.Column("slot_left_at", sa.DateTime(), nullable=True),
            sa.Column("slot_camera_id", sa.String(length=50), nullable=True),
            sa.Column("slot_snapshot_path", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["vehicle_id"], ["vehicles.id"], ondelete="NO ACTION"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_parking_sessions_plate_number", "parking_sessions", ["plate_number"], unique=False)
        op.create_index("ix_parking_sessions_entry_time", "parking_sessions", ["entry_time"], unique=False)
        op.create_index("ix_parking_sessions_exit_time", "parking_sessions", ["exit_time"], unique=False)
        op.create_index("ix_parking_sessions_zone_id", "parking_sessions", ["zone_id"], unique=False)
        op.create_index("ix_parking_sessions_status", "parking_sessions", ["status"], unique=False)


def downgrade() -> None:
    if _table_exists("parking_sessions"):
        op.drop_index("ix_parking_sessions_status", table_name="parking_sessions")
        op.drop_index("ix_parking_sessions_zone_id", table_name="parking_sessions")
        op.drop_index("ix_parking_sessions_exit_time", table_name="parking_sessions")
        op.drop_index("ix_parking_sessions_entry_time", table_name="parking_sessions")
        op.drop_index("ix_parking_sessions_plate_number", table_name="parking_sessions")
        op.drop_table("parking_sessions")

    vehicle_columns = _column_names("vehicles")
    if "email" in vehicle_columns:
        op.drop_column("vehicles", "email")
    if "phone" in vehicle_columns:
        op.drop_column("vehicles", "phone")
    if "is_employee" in vehicle_columns:
        op.drop_column("vehicles", "is_employee")

    alert_columns = _column_names("alerts")
    if "location_display" in alert_columns:
        op.drop_column("alerts", "location_display")
    if "severity" in alert_columns:
        op.drop_column("alerts", "severity")
