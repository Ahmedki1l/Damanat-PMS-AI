"""drop_fk_alert_vehicle_constraint

Drops the FK_alert_vehicle foreign key from the alerts table.
This constraint incorrectly requires alert.plate_number to exist in
vehicles.plate_number, which prevents creating alerts for unknown/unregistered
vehicles — a core use-case of the UNKNOWN_VEHICLE alert type.

Revision ID: 7a8b9c0d1e2f
Revises: 5c64d93ee242
Create Date: 2026-04-13 05:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '7a8b9c0d1e2f'
down_revision: Union[str, Sequence[str], None] = '3e94f140f526'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _foreign_key_names(table_name: str) -> set:
    bind = op.get_bind()
    inspector = inspect(bind)
    return {fk['name'] for fk in inspector.get_foreign_keys(table_name) if fk.get('name')}


def upgrade() -> None:
    """Drop the FK_alert_vehicle constraint if it exists."""
    if 'FK_alert_vehicle' in _foreign_key_names('alerts'):
        op.drop_constraint('FK_alert_vehicle', 'alerts', type_='foreignkey')


def downgrade() -> None:
    """Recreate the FK_alert_vehicle constraint (not recommended)."""
    # This constraint should not be re-added as it breaks unknown vehicle alerting.
    pass
