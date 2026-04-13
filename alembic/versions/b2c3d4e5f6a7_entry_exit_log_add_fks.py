"""entry_exit_log add foreign keys

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-08 13:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add FK constraints to entry_exit_log.vehicle_id and matched_entry_id."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Only add FKs if table exists and FKs don't already exist
    if 'entry_exit_log' not in inspector.get_table_names():
        return

    existing_fks = {fk['name'] for fk in inspector.get_foreign_keys('entry_exit_log')}

    if 'fk_entry_exit_log_vehicle_id' not in existing_fks:
        op.create_foreign_key(
            'fk_entry_exit_log_vehicle_id',
            'entry_exit_log', 'vehicles',
            ['vehicle_id'], ['id'],
            ondelete='NO ACTION',
        )

    if 'fk_entry_exit_log_matched_entry_id' not in existing_fks:
        op.create_foreign_key(
            'fk_entry_exit_log_matched_entry_id',
            'entry_exit_log', 'entry_exit_log',
            ['matched_entry_id'], ['id'],
            ondelete='NO ACTION',
        )


def downgrade() -> None:
    """Remove FK constraints."""
    op.drop_constraint('fk_entry_exit_log_matched_entry_id', 'entry_exit_log', type_='foreignkey')
    op.drop_constraint('fk_entry_exit_log_vehicle_id', 'entry_exit_log', type_='foreignkey')
