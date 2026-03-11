"""add region_id to alerts

Revision ID: d3e4f5a6b7c8
Revises: b2c3d4e5f6a7
Create Date: 2026-03-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add alerts.region_id (nullable)."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c["name"] for c in inspector.get_columns("alerts")}
    if "region_id" not in columns:
        op.add_column("alerts", sa.Column("region_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Drop alerts.region_id."""
    op.drop_column("alerts", "region_id")