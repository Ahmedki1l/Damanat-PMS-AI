"""add hik_validations

HikCentral evidence behind one gate event. A side table on purpose, so
`parking_sessions` keeps its shape and the integration can be removed without a
destructive migration.

Revision ID: d7f3a91c4be2
Revises: 5aa7ca8a676a
Create Date: 2026-07-27 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7f3a91c4be2"
down_revision: Union[str, Sequence[str], None] = "5aa7ca8a676a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create hik_validations."""
    op.create_table(
        "hik_validations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=True),
        sa.Column("entry_exit_log_id", sa.Integer(), nullable=True),
        sa.Column("direction", sa.String(length=20), nullable=False),
        sa.Column("guid", sa.String(length=64), nullable=False),
        sa.Column("plate_license", sa.String(length=50), nullable=True),
        sa.Column("canonical_plate", sa.String(length=50), nullable=True),
        sa.Column("reported_plate", sa.String(length=50), nullable=True),
        sa.Column("plate_source", sa.String(length=30), nullable=False),
        sa.Column("pass_time", sa.DateTime(), nullable=True),
        sa.Column("resource_id", sa.String(length=50), nullable=True),
        sa.Column("resource_name", sa.String(length=100), nullable=True),
        sa.Column("vehicle_image_path", sa.Text(), nullable=True),
        sa.Column("plate_image_path", sa.Text(), nullable=True),
        sa.Column("vehicle_type", sa.String(length=50), nullable=True),
        sa.Column("vehicle_direction_type", sa.String(length=50), nullable=True),
        sa.Column("matched", sa.Boolean(), nullable=False),
        sa.Column("match_reason", sa.String(length=50), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"], ["parking_sessions.id"], ondelete="NO ACTION"
        ),
        sa.ForeignKeyConstraint(
            ["entry_exit_log_id"], ["entry_exit_log.id"], ondelete="NO ACTION"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # HikCentral's GUID identifies one vehicle pass. Unique so a single platform
    # record can never justify two parking sessions.
    op.create_index(
        "ix_hik_validations_guid", "hik_validations", ["guid"], unique=True
    )
    op.create_index(
        "ix_hik_validations_session_id", "hik_validations", ["session_id"]
    )
    op.create_index(
        "ix_hik_validations_entry_exit_log_id",
        "hik_validations",
        ["entry_exit_log_id"],
    )
    op.create_index(
        "ix_hik_validations_canonical_plate",
        "hik_validations",
        ["canonical_plate"],
    )
    op.create_index(
        "ix_hik_validations_pass_time", "hik_validations", ["pass_time"]
    )


def downgrade() -> None:
    """Drop hik_validations."""
    op.drop_index("ix_hik_validations_pass_time", table_name="hik_validations")
    op.drop_index(
        "ix_hik_validations_canonical_plate", table_name="hik_validations"
    )
    op.drop_index(
        "ix_hik_validations_entry_exit_log_id", table_name="hik_validations"
    )
    op.drop_index("ix_hik_validations_session_id", table_name="hik_validations")
    op.drop_index("ix_hik_validations_guid", table_name="hik_validations")
    op.drop_table("hik_validations")
