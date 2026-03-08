"""vehicle is_registered integer to boolean

Revision ID: a1b2c3d4e5f6
Revises: cfe097758fdd
Create Date: 2026-03-08 12:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'cfe097758fdd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Change vehicles.is_registered from Integer to Boolean."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    columns = {c['name']: c for c in inspector.get_columns('vehicles')}
    col = columns.get('is_registered')
    if col and not isinstance(col['type'], sa.Boolean):
        # Drop the integer default before type change, then set boolean default
        op.alter_column('vehicles', 'is_registered', server_default=None)
        op.alter_column('vehicles', 'is_registered',
                        existing_type=sa.INTEGER(),
                        type_=sa.Boolean(),
                        existing_nullable=False,
                        postgresql_using='is_registered::boolean')
        op.alter_column('vehicles', 'is_registered', server_default='true')


def downgrade() -> None:
    """Revert vehicles.is_registered back to Integer."""
    op.alter_column('vehicles', 'is_registered', server_default=None)
    op.alter_column('vehicles', 'is_registered',
                    existing_type=sa.Boolean(),
                    type_=sa.INTEGER(),
                    existing_nullable=False,
                    postgresql_using='is_registered::int')
    op.alter_column('vehicles', 'is_registered', server_default='1')
