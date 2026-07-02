"""entry_exit_log_add_plate_confidence

Revision ID: 5aa7ca8a676a
Revises: 5e3ffe1ec281
Create Date: 2026-06-30 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5aa7ca8a676a'
down_revision: Union[str, Sequence[str], None] = '5e3ffe1ec281'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('entry_exit_log', sa.Column('plate_confidence', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('entry_exit_log', 'plate_confidence')
