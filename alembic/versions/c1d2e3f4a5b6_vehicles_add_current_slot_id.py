"""vehicles_add_current_slot_id

Revision ID: c1d2e3f4a5b6
Revises: 7a8b9c0d1e2f
Create Date: 2026-04-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = '7a8b9c0d1e2f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add vehicles.current_slot_id — the slot the vehicle is currently
    occupying, kept in sync by parking_session_service.bind_slot /
    unbind_slot. Mirrors the varchar key shape already used by
    parking_sessions.slot_id (e.g. 'B1_CRO'), so the value matches
    parking_slots.slot_id directly."""
    op.add_column(
        'vehicles',
        sa.Column('current_slot_id', sa.String(length=50), nullable=True),
    )
    op.create_index(
        'ix_vehicles_current_slot_id',
        'vehicles',
        ['current_slot_id'],
    )


def downgrade() -> None:
    """Drop the index and column."""
    op.drop_index('ix_vehicles_current_slot_id', table_name='vehicles')
    op.drop_column('vehicles', 'current_slot_id')
