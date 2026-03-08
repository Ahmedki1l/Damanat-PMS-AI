"""initial schema

Revision ID: cfe097758fdd
Revises:
Create Date: 2026-03-08 11:22:36.538468

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cfe097758fdd'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create all tables. Idempotent — skips tables that already exist (legacy DBs)."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    existing = set(inspector.get_table_names())

    if 'camera_events' not in existing:
        op.create_table(
            'camera_events',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('camera_id', sa.String(50), nullable=False),
            sa.Column('device_serial', sa.String(100), nullable=True),
            sa.Column('channel_id', sa.Integer(), nullable=True),
            sa.Column('event_type', sa.String(100), nullable=False),
            sa.Column('event_state', sa.String(20), nullable=True),
            sa.Column('event_description', sa.String(200), nullable=True),
            sa.Column('detection_target', sa.String(50), nullable=True),
            sa.Column('region_id', sa.String(100), nullable=True),
            sa.Column('channel_name', sa.String(100), nullable=True),
            sa.Column('trigger_time', sa.DateTime(), nullable=True),
            sa.Column('snapshot_path', sa.String(500), nullable=True),
            sa.Column('raw_payload', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_camera_events_camera_id', 'camera_events', ['camera_id'])
        op.create_index('ix_camera_events_event_type', 'camera_events', ['event_type'])
        op.create_index('ix_camera_events_created_at', 'camera_events', ['created_at'])

    if 'zone_occupancy' not in existing:
        op.create_table(
            'zone_occupancy',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('zone_id', sa.String(100), nullable=False),
            sa.Column('camera_id', sa.String(50), nullable=False),
            sa.Column('current_count', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('max_capacity', sa.Integer(), nullable=False, server_default='10'),
            sa.Column('last_updated', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('zone_id'),
        )
        op.create_index('ix_zone_occupancy_zone_id', 'zone_occupancy', ['zone_id'])

    if 'alerts' not in existing:
        op.create_table(
            'alerts',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('alert_type', sa.String(50), nullable=False),
            sa.Column('camera_id', sa.String(50), nullable=False),
            sa.Column('zone_id', sa.String(100), nullable=True),
            sa.Column('event_type', sa.String(100), nullable=True),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('is_resolved', sa.Boolean(), nullable=False, server_default='false'),
            sa.Column('triggered_at', sa.DateTime(), nullable=False),
            sa.Column('resolved_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_alerts_alert_type', 'alerts', ['alert_type'])
        op.create_index('ix_alerts_triggered_at', 'alerts', ['triggered_at'])
    else:
        # Legacy DB: alerts exists but is_resolved may be Integer — alter to Boolean
        columns = {c['name']: c for c in inspector.get_columns('alerts')}
        is_resolved_col = columns.get('is_resolved')
        if is_resolved_col and not isinstance(is_resolved_col['type'], sa.Boolean):
            op.alter_column('alerts', 'is_resolved',
                            existing_type=sa.INTEGER(),
                            type_=sa.Boolean(),
                            existing_nullable=False,
                            postgresql_using='is_resolved::boolean')

    if 'vehicles' not in existing:
        op.create_table(
            'vehicles',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('plate_number', sa.String(50), nullable=False),
            sa.Column('owner_name', sa.String(200), nullable=False),
            sa.Column('vehicle_type', sa.String(50), nullable=False),
            sa.Column('employee_id', sa.String(100), nullable=True),
            sa.Column('is_registered', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('registered_at', sa.DateTime(), nullable=True),
            sa.Column('notes', sa.Text(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('plate_number'),
        )
        op.create_index('ix_vehicles_plate_number', 'vehicles', ['plate_number'])

    if 'entry_exit_log' not in existing:
        op.create_table(
            'entry_exit_log',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('plate_number', sa.String(50), nullable=False),
            sa.Column('vehicle_id', sa.Integer(), nullable=True),
            sa.Column('vehicle_type', sa.String(50), nullable=True),
            sa.Column('gate', sa.String(20), nullable=False),
            sa.Column('camera_id', sa.String(50), nullable=False),
            sa.Column('event_time', sa.DateTime(), nullable=False),
            sa.Column('parking_duration', sa.Integer(), nullable=True),
            sa.Column('matched_entry_id', sa.Integer(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
        )
        op.create_index('ix_entry_exit_log_plate_number', 'entry_exit_log', ['plate_number'])
        op.create_index('ix_entry_exit_log_event_time', 'entry_exit_log', ['event_time'])


def downgrade() -> None:
    """Drop all tables."""
    op.drop_table('entry_exit_log')
    op.drop_table('vehicles')
    op.drop_table('alerts')
    op.drop_table('zone_occupancy')
    op.drop_table('camera_events')
