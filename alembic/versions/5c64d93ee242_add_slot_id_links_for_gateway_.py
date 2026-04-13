"""add_slot_id_links_for_gateway_normalization

Revision ID: 5c64d93ee242
Revises: 4b53f82cd131
Create Date: 2026-04-12 16:30:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "5c64d93ee242"
down_revision: Union[str, Sequence[str], None] = "4b53f82cd131"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def _column_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {col["name"] for col in inspector.get_columns(table_name)}


def _foreign_key_names(table_name: str) -> set[str]:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {fk["name"] for fk in inspector.get_foreign_keys(table_name) if fk.get("name")}


def upgrade() -> None:
    if _table_exists("alerts"):
        alert_columns = _column_names("alerts")
        if "slot_id" not in alert_columns:
            op.add_column("alerts", sa.Column("slot_id", sa.String(length=50), nullable=True))
        op.create_index("ix_alerts_slot_id", "alerts", ["slot_id"], unique=False, if_not_exists=True)

    if _table_exists("parking_sessions"):
        session_columns = _column_names("parking_sessions")
        if "slot_id" not in session_columns:
            op.add_column("parking_sessions", sa.Column("slot_id", sa.String(length=50), nullable=True))
        op.create_index("ix_parking_sessions_slot_id", "parking_sessions", ["slot_id"], unique=False, if_not_exists=True)

    if _table_exists("alerts") and _table_exists("parking_slots"):
        if "fk_alerts_slot_id" not in _foreign_key_names("alerts"):
            op.execute("""
                IF NOT EXISTS (
                    SELECT 1
                    FROM alerts a
                    LEFT JOIN parking_slots ps ON ps.slot_id = a.slot_id
                    WHERE a.slot_id IS NOT NULL AND ps.slot_id IS NULL
                )
                ALTER TABLE alerts
                ADD CONSTRAINT fk_alerts_slot_id
                FOREIGN KEY (slot_id) REFERENCES parking_slots (slot_id)
            """)

    if _table_exists("parking_sessions") and _table_exists("parking_slots"):
        if "fk_parking_sessions_slot_id" not in _foreign_key_names("parking_sessions"):
            op.execute("""
                IF NOT EXISTS (
                    SELECT 1
                    FROM parking_sessions psn
                    LEFT JOIN parking_slots ps ON ps.slot_id = psn.slot_id
                    WHERE psn.slot_id IS NOT NULL AND ps.slot_id IS NULL
                )
                ALTER TABLE parking_sessions
                ADD CONSTRAINT fk_parking_sessions_slot_id
                FOREIGN KEY (slot_id) REFERENCES parking_slots (slot_id)
            """)


def downgrade() -> None:
    if _table_exists("parking_sessions"):
        if "fk_parking_sessions_slot_id" in _foreign_key_names("parking_sessions"):
            op.drop_constraint("fk_parking_sessions_slot_id", "parking_sessions", type_="foreignkey")
        if "slot_id" in _column_names("parking_sessions"):
            op.drop_index("ix_parking_sessions_slot_id", table_name="parking_sessions")
            op.drop_column("parking_sessions", "slot_id")

    if _table_exists("alerts"):
        if "fk_alerts_slot_id" in _foreign_key_names("alerts"):
            op.drop_constraint("fk_alerts_slot_id", "alerts", type_="foreignkey")
        if "slot_id" in _column_names("alerts"):
            op.drop_index("ix_alerts_slot_id", table_name="alerts")
            op.drop_column("alerts", "slot_id")
