"""add slot_number to alerts

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-03-13 19:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "d3e4f5a6b7c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add alerts.slot_number — real-life parking bay number (nullable)."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("alerts")}
    if "slot_number" not in columns:
        op.add_column("alerts", sa.Column("slot_number", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop alerts.slot_number."""
    op.drop_column("alerts", "slot_number")
