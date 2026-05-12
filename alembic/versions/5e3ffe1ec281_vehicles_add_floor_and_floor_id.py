"""vehicles_add_floor_and_floor_id

Revision ID: 5e3ffe1ec281
Revises: c1d2e3f4a5b6
Create Date: 2026-05-12 07:10:55.244592

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '5e3ffe1ec281'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('vehicles', sa.Column('floor', sa.String(length=50), nullable=True))
    op.add_column('vehicles', sa.Column('floor_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('vehicles', 'floor_id')
    op.drop_column('vehicles', 'floor')
