"""Add durable successful entry-decision receipts.

Revision ID: e8a24c91d607
Revises: d7f3a91c4be2
"""

from alembic import op
import sqlalchemy as sa

revision = "e8a24c91d607"
down_revision = "d7f3a91c4be2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    if sa.inspect(connection).has_table("entry_confirmation_receipts"):
        return
    decision_type = sa.Unicode(200)
    if connection.dialect.name == "mssql":
        decision_type = sa.Unicode(200, collation="Latin1_General_100_BIN2")
    op.create_table(
        "entry_confirmation_receipts",
        sa.Column("decision_id", decision_type, primary_key=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("plate_number", sa.String(50), nullable=False),
        sa.Column("entry_log_id", sa.Integer(), sa.ForeignKey("entry_exit_log.id", ondelete="NO ACTION"), nullable=False),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("parking_sessions.id", ondelete="NO ACTION"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    raise RuntimeError(
        "Entry receipts are audit records; disable the feature flag for application rollback. "
        "Automatic receipt deletion is not supported."
    )
